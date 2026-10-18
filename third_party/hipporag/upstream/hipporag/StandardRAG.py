import os
import logging
from dataclasses import asdict
from typing import List, Dict, Tuple
import numpy as np
from tqdm import tqdm
import time

from .llm import _get_llm_class, BaseLLM
from .embedding_model import _get_embedding_model_class, BaseEmbeddingModel
from .embedding_store import get_embedding_store
from .evaluation.retrieval_eval import RetrievalRecall
from .evaluation.qa_eval import QAExactMatch, QAF1Score
from .prompts.linking import get_query_instruction
from .prompts.prompt_template_manager import PromptTemplateManager
from .utils.misc_utils import QuerySolution, ensure_list_input, min_max_normalize, validate_parallel_input_lengths
from .utils.config_utils import BaseConfig
from .utils.logging_utils import redact_config
from .utils.state_utils import component_class_identity, embedding_index_identity, validate_or_create_index_manifest

logger = logging.getLogger(__name__)

class StandardRAG:

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        closed_ids = getattr(self, "_closed_resource_ids", set())
        for resource in (getattr(self, "chunk_embedding_store", None), getattr(self, "embedding_model", None), getattr(self, "llm_model", None)):
            if resource is None or id(resource) in closed_ids:
                continue
            close = getattr(resource, "close", None)
            if callable(close):
                close()
            closed_ids.add(id(resource))
        self._closed_resource_ids = closed_ids

    def _construct_or_cleanup(self, factory):
        try:
            return factory()
        except Exception:
            self.close()
            raise

    def __init__(self,
                 global_config=None,
                 save_dir=None,
                 llm_model_name=None,
                 embedding_model_name=None,
                 llm_base_url=None,
                 azure_endpoint=None,
                 azure_embedding_endpoint=None,
                 azure_api_version=None,
                 azure_chat_deployment=None,
                 azure_embedding_api_version=None,
                 azure_embedding_deployment=None,
                 embedding_base_url=None,
                 embedding_provider=None):
        """
        """
        if global_config is None:
            self.global_config = BaseConfig()
        else:
            self.global_config = global_config

        #Overwriting Configuration if Specified
        if save_dir is not None:
            self.global_config.save_dir = save_dir

        if llm_model_name is not None:
            self.global_config.llm_name = llm_model_name

        if embedding_model_name is not None:
            self.global_config.embedding_model_name = embedding_model_name

        if llm_base_url is not None:
            self.global_config.llm_base_url = llm_base_url

        if embedding_base_url is not None:
            self.global_config.embedding_base_url = embedding_base_url

        if azure_endpoint is not None:
            self.global_config.azure_endpoint = azure_endpoint

        if azure_embedding_endpoint is not None:
            self.global_config.azure_embedding_endpoint = azure_embedding_endpoint

        if azure_api_version is not None:
            self.global_config.azure_api_version = azure_api_version

        if azure_chat_deployment is not None:
            self.global_config.azure_chat_deployment = azure_chat_deployment

        if azure_embedding_api_version is not None:
            self.global_config.azure_embedding_api_version = azure_embedding_api_version

        if azure_embedding_deployment is not None:
            self.global_config.azure_embedding_deployment = azure_embedding_deployment

        if embedding_provider is not None:
            self.global_config.embedding_provider = embedding_provider

        self.global_config.validate()

        _print_config = ",\n  ".join([f"{k} = {v}" for k, v in redact_config(asdict(self.global_config)).items()])
        logger.debug(f"StandardRAG init with config:\n  {_print_config}\n")

        #LLM and embedding model specific working directories are created under every specified saving directories
        llm_label = self.global_config.llm_name.replace("/", "_")
        embedding_label = self.global_config.embedding_model_name.replace("/", "_")
        self.working_dir = os.path.join(self.global_config.save_dir, f"{llm_label}_{embedding_label}")

        if not os.path.exists(self.working_dir):
            logger.info(f"Creating working directory: {self.working_dir}")
            os.makedirs(self.working_dir, exist_ok=True)

        self.llm_model: BaseLLM = self._construct_or_cleanup(lambda: _get_llm_class(self.global_config))

        # StandardRAG does not run OpenIE and always needs passage embeddings.
        self.embedding_model: BaseEmbeddingModel = self._construct_or_cleanup(
            lambda: _get_embedding_model_class(
                embedding_model_name=self.global_config.embedding_model_name,
                provider=self.global_config.embedding_provider,
            )(
                global_config=self.global_config,
                embedding_model_name=self.global_config.embedding_model_name,
            )
        )

        self.chunk_embedding_store = self._construct_or_cleanup(
            lambda: get_embedding_store(
                self.embedding_model,
                os.path.join(self.working_dir, "chunk_embeddings"),
                self.global_config.embedding_batch_size,
                'chunk',
                self.global_config,
            )
        )
        self.index_manifest_path = os.path.join(self.working_dir, "index_manifest.json")
        try:
            validate_or_create_index_manifest(
                self.index_manifest_path,
                {
                    "schema_version": 4,
                    "rag_type": "standard",
                    "embedding": embedding_index_identity(self.global_config),
                    "embedding_model_class": component_class_identity(self.embedding_model),
                },
                (self.chunk_embedding_store,),
            )
        except Exception:
            self.close()
            raise
        self.prompt_template_manager = self._construct_or_cleanup(
            lambda: PromptTemplateManager(role_mapping={"system": "system", "user": "user", "assistant": "assistant"})
        )

        self.ppr_time = 0
        self.rerank_time = 0
        self.all_retrieval_time = 0
        self._invalidate_retrieval_objects()

    def _invalidate_retrieval_objects(self) -> None:
        """Discard every in-memory retrieval snapshot after store mutation."""
        self.ready_to_retrieve = False
        self.query_to_embedding: Dict = {'triple': {}, 'passage': {}}
        self.passage_node_keys: List = []
        self.passage_embeddings = np.empty((0, 0), dtype=np.float32)

    def index(self, docs: List[str]):
        """
        Indexes the given documents based on the HippoRAG 2 framework which generates an OpenIE knowledge graph
        based on the given documents and encodes passages, entities and facts separately for later retrieval.

        Parameters:
            docs : List[str]
                A list of documents to be indexed.
        """

        ensure_list_input(docs, "docs")
        self._invalidate_retrieval_objects()
        logger.info(f"Indexing Documents")

        self.chunk_embedding_store.insert_strings(docs)

    def delete(self, docs_to_delete: List[str]):
        """

        """

        ensure_list_input(docs_to_delete, "docs_to_delete")
        #Making sure that all the necessary structures have been built.
        if not self.ready_to_retrieve:
            self.prepare_retrieval_objects()

        current_docs = set(self.chunk_embedding_store.get_all_texts())
        docs_to_delete = [doc for doc in docs_to_delete if doc in current_docs]

        #Get ids for chunks to delete
        chunk_ids_to_delete = set(
            [self.chunk_embedding_store.text_to_hash_id[chunk] for chunk in docs_to_delete])
        if not chunk_ids_to_delete:
            return

        logger.info(f"Deleting {len(chunk_ids_to_delete)} Chunks")

        self._invalidate_retrieval_objects()
        self.chunk_embedding_store.delete(chunk_ids_to_delete)

    def retrieve(self,
                     queries: List[str],
                     num_to_retrieve: int = None,
                     gold_docs: List[List[str]] = None) -> List[QuerySolution] | Tuple[List[QuerySolution], Dict]:
        """
        Performs retrieval using a DPR framework, which consists of several steps:
        - Dense passage scoring

        Parameters:
            queries: List[str]
                A list of query strings for which documents are to be retrieved.
            num_to_retrieve: int, optional
                The maximum number of documents to retrieve for each query. If not specified, defaults to
                the `retrieval_top_k` value defined in the global configuration.
            gold_docs: List[List[str]], optional
                A list of lists containing gold-standard documents corresponding to each query. Required
                if retrieval performance evaluation is enabled (`do_eval_retrieval` in global configuration).

        Returns:
            List[QuerySolution] or (List[QuerySolution], Dict)
                If retrieval performance evaluation is not enabled, returns a list of QuerySolution objects, each containing
                the retrieved documents and their scores for the corresponding query. If evaluation is enabled, also returns
                a dictionary containing the evaluation metrics computed over the retrieved results.

        Notes
        -----
        - Long queries with no relevant facts after reranking will default to results from dense passage retrieval.
        """
        ensure_list_input(queries, "queries")
        validate_parallel_input_lengths(queries, gold_docs=gold_docs)
        retrieve_start_time = time.time()  # Record start time

        if num_to_retrieve is None:
            num_to_retrieve = self.global_config.retrieval_top_k
        if not isinstance(num_to_retrieve, int) or num_to_retrieve < 1:
            raise ValueError("num_to_retrieve must be a positive integer.")

        if gold_docs is not None:
            retrieval_recall_evaluator = RetrievalRecall(global_config=self.global_config)

        if not self.ready_to_retrieve:
            self.prepare_retrieval_objects()

        self.get_query_embeddings(queries)

        retrieval_results = []

        for q_idx, query in tqdm(enumerate(queries), desc="Retrieving", total=len(queries)):
            logger.info('No facts found after reranking, return DPR results')
            sorted_doc_ids, sorted_doc_scores = self.dense_passage_retrieval(query)

            top_k_docs = [self.chunk_embedding_store.get_row(self.passage_node_keys[idx])["content"] for idx in
                          sorted_doc_ids[:num_to_retrieve]]

            retrieval_results.append(
                QuerySolution(question=query, docs=top_k_docs, doc_scores=sorted_doc_scores[:num_to_retrieve]))

        retrieve_end_time = time.time()  # Record end time

        self.all_retrieval_time += retrieve_end_time - retrieve_start_time

        logger.info(f"Total Retrieval Time {self.all_retrieval_time:.2f}s")

        # Evaluate retrieval
        if gold_docs is not None:
            k_list = [1, 2, 5, 10, 20, 30, 50, 100, 150, 200]
            overall_retrieval_result, example_retrieval_results = retrieval_recall_evaluator.calculate_metric_scores(
                gold_docs=gold_docs, retrieved_docs=[retrieval_result.docs for retrieval_result in retrieval_results],
                k_list=k_list)
            logger.info(f"Evaluation results for retrieval: {overall_retrieval_result}")

            return retrieval_results, overall_retrieval_result
        else:
            return retrieval_results

    def rag_qa(self,
               queries: List[str|QuerySolution],
               gold_docs: List[List[str]] = None,
               gold_answers: List[List[str]] = None) -> Tuple[List[QuerySolution], List[str], List[Dict]] | Tuple[List[QuerySolution], List[str], List[Dict], Dict, Dict]:
        """
        Performs retrieval-augmented generation enhanced QA using a standard DPR framework.

        This method can handle both string-based queries and pre-processed QuerySolution objects. Depending
        on its inputs, it returns answers only or additionally evaluate retrieval and answer quality using
        recall @ k, exact match and F1 score metrics.

        Parameters:
            queries (List[Union[str, QuerySolution]]): A list of queries, which can be either strings or
                QuerySolution instances. If they are strings, retrieval will be performed.
            gold_docs (Optional[List[List[str]]]): A list of lists containing gold-standard documents for
                each query. This is used if document-level evaluation is to be performed. Default is None.
            gold_answers (Optional[List[List[str]]]): A list of lists containing gold-standard answers for
                each query. Required if evaluation of question answering (QA) answers is enabled. Default
                is None.

        Returns:
            Union[
                Tuple[List[QuerySolution], List[str], List[Dict]],
                Tuple[List[QuerySolution], List[str], List[Dict], Dict, Dict]
            ]: A tuple that always includes:
                - List of QuerySolution objects containing answers and metadata for each query.
                - List of response messages for the provided queries.
                - List of metadata dictionaries for each query.
                If evaluation is enabled, the tuple also includes:
                - A dictionary with overall results from the retrieval phase (if applicable).
                - A dictionary with overall QA evaluation metrics (exact match and F1 scores).

        """
        ensure_list_input(queries, "queries")
        validate_parallel_input_lengths(queries, gold_docs=gold_docs, gold_answers=gold_answers)
        if gold_answers is not None:
            qa_em_evaluator = QAExactMatch(global_config=self.global_config)
            qa_f1_evaluator = QAF1Score(global_config=self.global_config)

        # Retrieving (if necessary)
        overall_retrieval_result = None

        if queries and not isinstance(queries[0], QuerySolution):
            if gold_docs is not None:
                queries, overall_retrieval_result = self.retrieve(queries=queries, gold_docs=gold_docs)
            else:
                queries = self.retrieve(queries=queries)

        # Performing QA
        queries_solutions, all_response_message, all_metadata = self.qa(queries)

        # Evaluating QA
        if gold_answers is not None:
            overall_qa_em_result, example_qa_em_results = qa_em_evaluator.calculate_metric_scores(
                gold_answers=gold_answers, predicted_answers=[qa_result.answer for qa_result in queries_solutions],
                aggregation_fn=np.max)
            overall_qa_f1_result, example_qa_f1_results = qa_f1_evaluator.calculate_metric_scores(
                gold_answers=gold_answers, predicted_answers=[qa_result.answer for qa_result in queries_solutions],
                aggregation_fn=np.max)

            # round off to 4 decimal places for QA results
            overall_qa_em_result.update(overall_qa_f1_result)
            overall_qa_results = overall_qa_em_result
            overall_qa_results = {k: round(float(v), 4) for k, v in overall_qa_results.items()}
            logger.info(f"Evaluation results for QA: {overall_qa_results}")

            # Save retrieval and QA results
            for idx, q in enumerate(queries_solutions):
                q.gold_answers = list(gold_answers[idx])
                if gold_docs is not None:
                    q.gold_docs = gold_docs[idx]

            return queries_solutions, all_response_message, all_metadata, overall_retrieval_result, overall_qa_results
        else:
            return queries_solutions, all_response_message, all_metadata

    def qa(self, queries: List[QuerySolution]) -> Tuple[List[QuerySolution], List[str], List[Dict]]:
        """
        Executes question-answering (QA) inference using a provided set of query solutions and a language model.

        Parameters:
            queries: List[QuerySolution]
                A list of QuerySolution objects that contain the user queries, retrieved documents, and other related information.

        Returns:
            Tuple[List[QuerySolution], List[str], List[Dict]]
                A tuple containing:
                - A list of updated QuerySolution objects with the predicted answers embedded in them.
                - A list of raw response messages from the language model.
                - A list of metadata dictionaries associated with the results.
        """
        ensure_list_input(queries, "queries")
        if not queries:
            return [], [], []
        #Running inference for QA
        all_qa_messages = []

        for query_solution in tqdm(queries, desc="Collecting QA prompts"):

            # obtain the retrieved docs
            retrieved_passages = query_solution.docs[:self.global_config.qa_top_k]

            prompt_user = ''
            for passage in retrieved_passages:
                prompt_user += f'Wikipedia Title: {passage}\n\n'
            prompt_user += 'Question: ' + query_solution.question + '\nThought: '

            if self.prompt_template_manager.is_template_name_valid(name=f'rag_qa_{self.global_config.dataset}'):
                # find the corresponding prompt for this dataset
                prompt_dataset_name = self.global_config.dataset
            else:
                # the dataset does not have a customized prompt template yet
                logger.warning(
                    f"rag_qa_{self.global_config.dataset} does not have a customized prompt template. Using MUSIQUE's prompt template instead.")
                prompt_dataset_name = 'musique'
            all_qa_messages.append(
                self.prompt_template_manager.render(name=f'rag_qa_{prompt_dataset_name}', prompt_user=prompt_user))

        all_qa_results = [self.llm_model.infer(qa_messages) for qa_messages in tqdm(all_qa_messages, desc="QA Reading")]

        all_response_message, all_metadata, all_cache_hit = zip(*all_qa_results)
        all_response_message, all_metadata = list(all_response_message), list(all_metadata)

        #Process responses and extract predicted answers.
        queries_solutions = []
        for query_solution_idx, query_solution in tqdm(enumerate(queries), desc="Extraction Answers from LLM Response"):
            response_content = all_response_message[query_solution_idx]
            try:
                pred_ans = response_content.split('Answer:')[1].strip()
            except Exception as e:
                logger.warning(f"Error in parsing the answer from the raw LLM QA inference response: {str(e)}!")
                pred_ans = response_content

            query_solution.answer = pred_ans
            queries_solutions.append(query_solution)

        return queries_solutions, all_response_message, all_metadata


    def prepare_retrieval_objects(self):
        """
        Prepares various in-memory objects and attributes necessary for fast retrieval processes, such as embedding data and graph relationships, ensuring consistency
        and alignment with the underlying graph structure.
        """

        logger.info("Preparing for fast retrieval.")

        logger.info("Loading keys.")
        self.query_to_embedding: Dict = {'triple': {}, 'passage': {}}

        self.passage_node_keys: List = list(self.chunk_embedding_store.get_all_ids()) # a list of passage node keys

        logger.info("Loading embeddings.")
        self.passage_embeddings = np.array(self.chunk_embedding_store.get_embeddings(self.passage_node_keys))

        self.ready_to_retrieve = True

    def get_query_embeddings(self, queries: List[str] | List[QuerySolution]):
        """
        Retrieves embeddings for given queries and updates the internal query-to-embedding mapping. The method determines whether each query
        is already present in the `self.query_to_embedding` dictionary under the keys 'triple' and 'passage'. If a query is not present in
        either, it is encoded into embeddings using the embedding model and stored.

        Args:
            queries List[str] | List[QuerySolution]: A list of query strings or QuerySolution objects. Each query is checked for
            its presence in the query-to-embedding mappings.
        """

        all_query_strings = []
        seen_queries = set()
        for query in queries:
            query_text = query.question if isinstance(query, QuerySolution) else query
            if not isinstance(query_text, str):
                raise TypeError(f"Queries must be strings or QuerySolution instances, got {type(query).__name__}.")
            if query_text not in self.query_to_embedding['passage'] and query_text not in seen_queries:
                all_query_strings.append(query_text)
                seen_queries.add(query_text)

        if len(all_query_strings) > 0:
            logger.info(f"Encoding {len(all_query_strings)} queries for query_to_passage.")
            encode_kwargs = {"norm": True}
            if getattr(self.embedding_model, "query_instruction_mode", "ignored") != "ignored":
                encode_kwargs["instruction"] = get_query_instruction('query_to_passage')
            query_embeddings_for_passage = self.embedding_model.batch_encode(all_query_strings, **encode_kwargs)
            for query, embedding in zip(all_query_strings, query_embeddings_for_passage):
                self.query_to_embedding['passage'][query] = embedding

    def dense_passage_retrieval(self, query: str) -> Tuple[np.ndarray, np.ndarray]:
        """
        Conduct dense passage retrieval to find relevant documents for a query.

        This function processes a given query using a pre-trained embedding model
        to generate query embeddings. The similarity scores between the query
        embedding and passage embeddings are computed using dot product, followed
        by score normalization. Finally, the function ranks the documents based
        on their similarity scores and returns the ranked document identifiers
        and their scores.

        Parameters
        ----------
        query : str
            The input query for which relevant passages should be retrieved.

        Returns
        -------
        tuple : Tuple[np.ndarray, np.ndarray]
            A tuple containing two elements:
            - A list of sorted document identifiers based on their relevance scores.
            - A numpy array of the normalized similarity scores for the corresponding
              documents.
        """
        if not self.ready_to_retrieve:
            self.prepare_retrieval_objects()
        if len(self.passage_embeddings) == 0:
            return np.asarray([], dtype=np.int64), np.asarray([], dtype=np.float32)
        if query not in self.query_to_embedding['passage']:
            self.get_query_embeddings([query])
        query_embedding = self.query_to_embedding['passage'][query]
        query_doc_scores = np.dot(self.passage_embeddings, query_embedding.T)
        query_doc_scores = np.squeeze(query_doc_scores) if query_doc_scores.ndim == 2 else query_doc_scores
        query_doc_scores = min_max_normalize(query_doc_scores)

        sorted_doc_ids = np.argsort(query_doc_scores)[::-1]
        sorted_doc_scores = query_doc_scores[sorted_doc_ids.tolist()]

        return sorted_doc_ids, sorted_doc_scores
