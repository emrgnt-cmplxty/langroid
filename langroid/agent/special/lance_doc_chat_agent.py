"""
LanceDocChatAgent is a subclass of DocChatAgent that uses LanceDB as a vector store:
- Uses the DocChatAgentConfig.filter variable
    (a sql string) in the `where` clause to do filtered vector search.
- Overrides the get_similar_chunks_bm25() to use LanceDB FTS (Full Text Search).

For usage see:
 - `tests/main/test_lance_doc_chat_agent.py`.
 - example script `examples/docqa/lance_rag.py`.

"""
import json
import logging
from typing import List, Tuple

import pandas as pd

from langroid.agent.special.doc_chat_agent import DocChatAgent, DocChatAgentConfig
from langroid.agent.special.lance_rag.lance_tools import QueryPlanTool
from langroid.mytypes import Document
from langroid.parsing.table_loader import describe_dataframe
from langroid.utils.constants import DONE, NO_ANSWER
from langroid.utils.pydantic_utils import (
    clean_schema,
    dataframe_to_documents,
)
from langroid.vector_store.lancedb import LanceDB

logger = logging.getLogger(__name__)


class LanceDocChatAgent(DocChatAgent):
    vecdb: LanceDB

    def __init__(self, cfg: DocChatAgentConfig):
        super().__init__(cfg)
        self.config: DocChatAgentConfig = cfg
        self.enable_message(QueryPlanTool, use=False, handle=True)

    def _get_clean_vecdb_schema(self) -> str:
        if self.from_dataframe:
            return self.df_description
        schema_dict = clean_schema(
            self.vecdb.schema,
            excludes=["id", "vector"],
        )
        # intersect config.filter_fields with schema_dict.keys() in case
        # there are extraneous fields in config.filter_fields
        filter_fields_set = set(
            self.config.filter_fields or schema_dict.keys()
        ).intersection(schema_dict.keys())

        # possible values of filterable fields
        filter_field_values = self.get_field_values(list(filter_fields_set))

        # add field values to schema_dict as another field `values` for each field
        for field, values in filter_field_values.items():
            if field in schema_dict:
                schema_dict[field]["values"] = values
        # if self.config.filter_fields is set, restrict to these:
        if len(self.config.filter_fields) > 0:
            schema_dict = {
                k: v for k, v in schema_dict.items() if k in self.config.filter_fields
            }
        schema = json.dumps(schema_dict, indent=4)
        if len(fields := self.config.add_fields_to_content) > 0:
            schema += f"""
            Additional fields added to `content` as key=value pairs:
            NOTE That CAN Help with matching queries!
            {fields}
            """
        return schema

    def query_plan(self, msg: QueryPlanTool) -> str:
        """
        Handle the LLM's use of the FilterTool.
        Temporarily set the config filter and invoke the DocChatAgent.llm_response()
        """
        # create document-subset based on this filter
        plan = msg.plan
        try:
            self.setup_documents(filter=plan.filter or None)
        except Exception as e:
            logger.error(f"Error setting up documents: {e}")
            # say DONE with err msg so it goes back to LanceFilterAgent
            return f"{DONE} Possible Filter Error:\n {e}"
        # update the filter so it is used in the DocChatAgent
        self.config.filter = plan.filter or None
        if plan.dataframe_calc:
            # we just get relevant docs then do the calculation
            # TODO if calc causes err, it is captured in result,
            # and LLM can correct the calc based on the err,
            # and this will cause retrieval all over again,
            # which may be wasteful if only the calc part is wrong.
            # The calc step can later be done with a separate Agent/Tool.
            _, docs = self.get_relevant_extracts(plan.query)
            if len(docs) == 0:
                return DONE + " " + NO_ANSWER
            result = self.vecdb.compute_from_docs(docs, plan.dataframe_calc)
            return DONE + " " + result
        else:
            # pass on the query so LLM can handle it
            return plan.query

    def ingest_docs(self, docs: List[Document], split: bool = True) -> int:
        n = super().ingest_docs(docs, split)
        tbl = self.vecdb.client.open_table(self.vecdb.config.collection_name)
        # We assume "content" is available as top-level field
        if "content" in tbl.schema.names:
            tbl.create_fts_index("content")
        return n

    def ingest_dataframe(
        self,
        df: pd.DataFrame,
        content: str = "content",
        metadata: List[str] = [],
    ) -> int:
        self.from_dataframe = True
        if df.shape[0] == 0:
            raise ValueError(
                """
                LanceDocChatAgent.ingest_dataframe() received an empty dataframe.
                """
            )
        n = df.shape[0]

        # If any additional fields need to be added to content,
        # add them as key=value pairs, into the `content` field for all rows.
        # This helps retrieval for table-like data.
        # Note we need to do this at stage so that the embeddings
        # are computed on the full content with these additional fields.
        fields = [f for f in self.config.add_fields_to_content if f in df.columns]
        if len(fields) > 0:
            df[content] = df.apply(
                lambda row: (",".join(f"{f}={row[f]}" for f in fields))
                + ", content="
                + row[content],
                axis=1,
            )

        df, metadata = DocChatAgent.document_compatible_dataframe(df, content, metadata)
        self.df_description = describe_dataframe(
            df,
            filter_fields=self.config.filter_fields,
            n_vals=10,
        )
        self.vecdb.add_dataframe(df, content="content", metadata=metadata)

        tbl = self.vecdb.client.open_table(self.vecdb.config.collection_name)
        # We assume "content" is available as top-level field
        if "content" in tbl.schema.names:
            tbl.create_fts_index("content")
        # We still need to do the below so that
        # other types of searches in DocChatAgent
        # can work, as they require Document objects
        docs = dataframe_to_documents(df, content="content", metadata=metadata)
        self.setup_documents(docs)
        # mark each doc as already-chunked so we don't try to split them further
        # TODO later we may want to split large text-columns
        for d in docs:
            d.metadata.is_chunk = True
        return n  # type: ignore

    def get_similar_chunks_bm25(
        self, query: str, multiple: int
    ) -> List[Tuple[Document, float]]:
        """
        Override the DocChatAgent.get_similar_chunks_bm25()
        to use LanceDB FTS (Full Text Search).
        """
        # replace all newlines with spaces in query
        query_clean = query.replace("\n", " ")
        # force special search keywords to lower case
        # so it's not interpreted as search syntax
        query_clean = (
            query_clean.replace("AND", "and").replace("OR", "or").replace("NOT", "not")
        )

        tbl = self.vecdb.client.open_table(self.vecdb.config.collection_name)
        result = (
            tbl.search(query_clean)
            .where(self.config.filter or None)
            .limit(self.config.parsing.n_similar_docs * multiple)
        )
        docs = self.vecdb._lance_result_to_docs(result)
        scores = [r["score"] for r in result.to_list()]
        return list(zip(docs, scores))
