"""Unmodified upstream graph methods; see PROVENANCE.json and LICENSE."""

from __future__ import annotations

import logging
import re
import time
from hashlib import md5
from typing import Dict, List, Optional, Tuple

import numpy as np
from tqdm import tqdm
from hipporag_knn import retrieve_knn

logger = logging.getLogger(__name__)


class StateConsistencyError(RuntimeError):
    pass


def text_processing(text):
    if isinstance(text, list):
        return [text_processing(t) for t in text]
    if not isinstance(text, str):
        text = str(text)
    normalized = ''.join(character if character.isalnum() or character.isspace() else ' ' for character in text.casefold())
    return ' '.join(normalized.split())


def min_max_normalize(x):
    min_val = np.min(x)
    max_val = np.max(x)
    range_val = max_val - min_val
    
    # Handle the case where all values are the same (range is zero)
    if range_val == 0:
        return np.ones_like(x)  # Return an array of ones with the same shape as x
    
    return (x - min_val) / range_val


def compute_mdhash_id(content: str, prefix: str = "") -> str:
    """
    Compute the MD5 hash of the given content string and optionally prepend a prefix.

    Args:
        content (str): The input string to be hashed.
        prefix (str, optional): A string to prepend to the resulting hash. Defaults to an empty string.

    Returns:
        str: A string consisting of the prefix followed by the hexadecimal representation of the MD5 hash.
    """
    return prefix + md5(content.encode()).hexdigest()


class HippoRAGGraphReference:

    def add_fact_edges(self, chunk_ids: List[str], chunk_triples: List[Tuple]):
        """
        Adds fact edges from given triples to the graph.

        The method processes chunks of triples, computes unique identifiers
        for entities and relations, and updates various internal statistics
        to build and maintain the graph structure. Entities are uniquely
        identified and linked based on their relationships.

        Parameters:
            chunk_ids: List[str]
                A list of unique identifiers for the chunks being processed.
            chunk_triples: List[Tuple]
                A list of tuples representing triples to process. Each triple
                consists of a subject, predicate, and object.

        Raises:
            Does not explicitly raise exceptions within the provided function logic.
        """

        if not hasattr(self, "_fact_edge_source_counts"):
            self._fact_edge_source_counts = {}
        if "name" in self.graph.vs.attribute_names():
            current_graph_nodes = set(self.graph.vs["name"])
        else:
            current_graph_nodes = set()

        logger.info(f"Adding OpenIE triples to graph.")

        for chunk_key, triples in tqdm(zip(chunk_ids, chunk_triples)):
            entities_in_chunk = set()

            for triple in triples:
                triple = tuple(triple)

                node_key = compute_mdhash_id(content=triple[0], prefix=("entity-"))
                node_2_key = compute_mdhash_id(content=triple[2], prefix=("entity-"))

                entities_in_chunk.add(node_key)
                entities_in_chunk.add(node_2_key)

                if chunk_key not in current_graph_nodes:
                    fact_edges = ((node_key, node_2_key), (node_2_key, node_key)) if self.graph.is_directed() else (tuple(sorted((node_key, node_2_key))),)
                    for fact_edge in fact_edges:
                        self.node_to_node_stats[fact_edge] = self.node_to_node_stats.get(fact_edge, 0.0) + 1
                        source_counts = self._fact_edge_source_counts.setdefault(fact_edge, {})
                        source_counts[chunk_key] = source_counts.get(chunk_key, 0) + 1

            for node in entities_in_chunk:
                self.ent_node_to_chunk_ids[node] = self.ent_node_to_chunk_ids.get(node, set()).union({chunk_key})


    def add_passage_edges(self, chunk_ids: List[str], chunk_triple_entities: List[List[str]]):
        """
        Adds edges connecting passage nodes to phrase nodes in the graph.

        This method is responsible for iterating through a list of chunk identifiers
        and their corresponding triple entities. It calculates and adds new edges
        between the passage nodes (defined by the chunk identifiers) and the phrase
        nodes (defined by the computed unique hash IDs of triple entities). The method
        also updates the node-to-node statistics map and keeps count of newly added
        passage nodes.

        Parameters:
            chunk_ids : List[str]
                A list of identifiers representing passage nodes in the graph.
            chunk_triple_entities : List[List[str]]
                A list of lists where each sublist contains entities (strings) associated
                with the corresponding chunk in the chunk_ids list.

        Returns:
            int
                The number of new passage nodes added to the graph.
        """

        if not hasattr(self, "_passage_edge_sources"):
            self._passage_edge_sources = {}
        if "name" in self.graph.vs.attribute_names():
            current_graph_nodes = set(self.graph.vs["name"])
        else:
            current_graph_nodes = set()

        num_new_chunks = 0

        logger.info(f"Connecting passage nodes to phrase nodes.")

        for idx, chunk_key in tqdm(enumerate(chunk_ids)):

            if chunk_key not in current_graph_nodes:
                for chunk_ent in chunk_triple_entities[idx]:
                    node_key = compute_mdhash_id(chunk_ent, prefix="entity-")

                    passage_edge = (chunk_key, node_key)
                    self.node_to_node_stats[passage_edge] = 1.0
                    self._passage_edge_sources[passage_edge] = chunk_key

                num_new_chunks += 1

        return num_new_chunks


    def add_synonymy_edges(self, query_node_keys: Optional[List[str]] = None):
        """
        Adds synonymy edges between similar nodes in the graph to enhance connectivity by identifying and linking synonym entities.

        This method performs key operations to compute and add synonymy edges. It first retrieves embeddings for all nodes, then conducts
        a nearest neighbor (KNN) search to find similar nodes. These similar nodes are identified based on a score threshold, and edges
        are added to represent the synonym relationship.

        Attributes:
            entity_id_to_row: dict (populated within the function). Maps each entity ID to its corresponding row data, where rows
                              contain `content` of entities used for comparison.
            entity_embedding_store: Manages retrieval of texts and embeddings for all rows related to entities.
            global_config: Configuration object that defines parameters such as `synonymy_edge_topk`, `synonymy_edge_sim_threshold`,
                           `synonymy_edge_query_batch_size`, and `synonymy_edge_key_batch_size`.
            node_to_node_stats: dict. Stores scores for edges between nodes representing their relationship.

        """
        logger.info(f"Expanding graph with synonymy edges")
        if not hasattr(self, "_synonym_edge_scores"):
            self._synonym_edge_scores = {}

        self.entity_id_to_row = self.entity_embedding_store.get_all_id_to_rows()
        entity_node_keys = list(self.entity_id_to_row.keys())
        if query_node_keys is None and hasattr(self, "_pending_synonymy_entity_ids"):
            query_node_keys = self._pending_synonymy_entity_ids
            del self._pending_synonymy_entity_ids
        if query_node_keys is None:
            query_node_keys = entity_node_keys
        else:
            query_node_keys = [node_key for node_key in query_node_keys if node_key in self.entity_id_to_row]

        logger.info(f"Performing KNN retrieval for {len(query_node_keys)} new phrase nodes against {len(entity_node_keys)} total nodes.")
        if not query_node_keys or not entity_node_keys:
            return

        query_entity_embs = self.entity_embedding_store.get_embeddings(query_node_keys)
        entity_embs = self.entity_embedding_store.get_embeddings(entity_node_keys)

        # Here we build synonymy edges only between newly inserted phrase nodes and all phrase nodes in the storage to reduce cost for incremental graph updates
        query_node_key2knn_node_keys = retrieve_knn(query_ids=query_node_keys,
                                                    key_ids=entity_node_keys,
                                                    query_vecs=query_entity_embs,
                                                    key_vecs=entity_embs,
                                                    k=self.global_config.synonymy_edge_topk,
                                                    query_batch_size=self.global_config.synonymy_edge_query_batch_size,
                                                    key_batch_size=self.global_config.synonymy_edge_key_batch_size)

        num_synonym_triple = 0
        synonym_candidates = []  # [(node key, [(synonym node key, corresponding score), ...]), ...]

        for node_key in tqdm(query_node_key2knn_node_keys.keys(), total=len(query_node_key2knn_node_keys)):
            synonyms = []

            entity = self.entity_id_to_row[node_key]["content"]

            if len(re.sub('[^A-Za-z0-9]', '', entity)) > 2:
                nns = query_node_key2knn_node_keys[node_key]

                num_nns = 0
                for nn, score in zip(nns[0], nns[1]):
                    if score < self.global_config.synonymy_edge_sim_threshold or num_nns >= 100:
                        break

                    nn_phrase = self.entity_id_to_row[nn]["content"]

                    if nn != node_key and nn_phrase != '':
                        synonyms.append((nn, score))
                        num_synonym_triple += 1
                        similarity_edges = ((node_key, nn), (nn, node_key)) if self.graph.is_directed() else (tuple(sorted((node_key, nn))),)
                        for sim_edge in similarity_edges:
                            self.node_to_node_stats[sim_edge] = max(self.node_to_node_stats.get(sim_edge, 0.0), score)
                            self._synonym_edge_scores[sim_edge] = max(self._synonym_edge_scores.get(sim_edge, 0.0), float(score))
                        num_nns += 1

            synonym_candidates.append((node_key, synonyms))


    def add_new_nodes(self):
        """
        Adds new nodes to the graph from entity and passage embedding stores based on their attributes.

        This method identifies and adds new nodes to the graph by comparing existing nodes
        in the graph and nodes retrieved from the entity embedding store and the passage
        embedding store. The method checks attributes and ensures no duplicates are added.
        New nodes are prepared and added in bulk to optimize graph updates.
        """

        existing_nodes = {v["name"]: v for v in self.graph.vs if "name" in v.attributes()}

        entity_to_row = self.entity_embedding_store.get_all_id_to_rows()
        passage_to_row = self.chunk_embedding_store.get_all_id_to_rows()

        node_to_rows = entity_to_row
        node_to_rows.update(passage_to_row)

        new_nodes = {}
        for node_id, node in node_to_rows.items():
            node['name'] = node_id
            if node_id not in existing_nodes:
                for k, v in node.items():
                    if k not in new_nodes:
                        new_nodes[k] = []
                    new_nodes[k].append(v)

        if len(new_nodes) > 0:
            self.graph.add_vertices(n=len(next(iter(new_nodes.values()))), attributes=new_nodes)


    def add_new_edges(self):
        """
        Merge typed edge contributions into the graph without creating parallel copies.

        `node_to_node_stats` only contains contributions from the current indexing pass.
        Existing fact sources and synonym scores therefore have to be merged by their
        logical `(source_key, target_key)` identity to make incremental and one-shot
        indexing produce the same weighted graph. Undirected edges use a canonical key
        so reverse entries cannot become parallel physical edges.
        """
        attribute_names = ("weight", "edge_kind", "fact_source_counts", "synonym_score", "passage_source", "source_key", "target_key")

        def logical_key(source_key, target_key):
            return (source_key, target_key) if self.graph.is_directed() else tuple(sorted((source_key, target_key)))

        def compose_metadata(source_key, target_key, fact_source_counts=None, synonym_score=0.0, passage_source=None):
            if fact_source_counts is None:
                fact_source_counts = {}
            if not isinstance(fact_source_counts, dict):
                raise StateConsistencyError(f"Invalid fact provenance for graph edge {source_key} -> {target_key}.")
            normalized_counts = {}
            for source, count in fact_source_counts.items():
                if not isinstance(source, str) or not isinstance(count, int) or isinstance(count, bool) or count < 1:
                    raise StateConsistencyError(f"Invalid fact source count for graph edge {source_key} -> {target_key}.")
                normalized_counts[source] = count
            synonym_score = float(synonym_score or 0.0)
            if synonym_score < 0:
                raise StateConsistencyError(f"Invalid synonym score for graph edge {source_key} -> {target_key}.")
            edge_kinds = []
            if normalized_counts:
                edge_kinds.append("fact")
            if passage_source is not None:
                edge_kinds.append("passage")
            if synonym_score > 0:
                edge_kinds.append("synonym")
            if not edge_kinds:
                raise StateConsistencyError(f"Graph edge {source_key} -> {target_key} has no typed contribution.")
            return {
                "weight": max(float(sum(normalized_counts.values())), 1.0 if passage_source is not None else 0.0, synonym_score),
                "edge_kind": "+".join(edge_kinds),
                "fact_source_counts": normalized_counts,
                "synonym_score": synonym_score,
                "passage_source": passage_source,
                "source_key": source_key,
                "target_key": target_key,
            }

        def merge_metadata(left, right):
            merged_counts = dict(left["fact_source_counts"])
            for source, count in right["fact_source_counts"].items():
                merged_counts[source] = max(merged_counts.get(source, 0), count)
            left_passage = left["passage_source"]
            right_passage = right["passage_source"]
            if left_passage is not None and right_passage is not None and left_passage != right_passage:
                raise StateConsistencyError(f"Conflicting passage provenance for graph edge {left['source_key']} -> {left['target_key']}.")
            return compose_metadata(
                left["source_key"],
                left["target_key"],
                merged_counts,
                max(left["synonym_score"], right["synonym_score"]),
                left_passage if left_passage is not None else right_passage,
            )

        current_node_ids = set(self.graph.vs["name"])
        pending_metadata = {}
        for edge in self.node_to_node_stats:
            if edge[0] == edge[1]:
                continue
            if edge[0] not in current_node_ids or edge[1] not in current_node_ids:
                logger.warning(f"Edge {edge[0]} -> {edge[1]} is not valid.")
                continue
            canonical_edge = logical_key(edge[0], edge[1])
            metadata = compose_metadata(
                canonical_edge[0],
                canonical_edge[1],
                dict(getattr(self, "_fact_edge_source_counts", {}).get(edge, {})),
                getattr(self, "_synonym_edge_scores", {}).get(edge, 0.0),
                getattr(self, "_passage_edge_sources", {}).get(edge),
            )
            pending_metadata[canonical_edge] = merge_metadata(pending_metadata[canonical_edge], metadata) if canonical_edge in pending_metadata else metadata

        existing_edges = {}
        duplicate_edge_indices = []
        required_existing_attributes = set(attribute_names).difference({"weight", "edge_kind"})
        for graph_edge in self.graph.es:
            if not required_existing_attributes.issubset(graph_edge.attributes()):
                raise StateConsistencyError("The graph is missing source-aware edge metadata. Rebuild with force_index_from_scratch=True.")
            source_key = graph_edge["source_key"]
            target_key = graph_edge["target_key"]
            if not isinstance(source_key, str) or not isinstance(target_key, str):
                raise StateConsistencyError("Graph edge source_key and target_key must be strings.")
            physical_edge = (self.graph.vs[graph_edge.source]["name"], self.graph.vs[graph_edge.target]["name"])
            endpoints_match = physical_edge == (source_key, target_key) if self.graph.is_directed() else set(physical_edge) == {source_key, target_key}
            if not endpoints_match:
                raise StateConsistencyError(f"Graph edge metadata {source_key} -> {target_key} does not match its physical endpoints {physical_edge}.")
            canonical_edge = logical_key(source_key, target_key)
            metadata = compose_metadata(
                canonical_edge[0],
                canonical_edge[1],
                graph_edge["fact_source_counts"],
                graph_edge["synonym_score"],
                graph_edge["passage_source"],
            )
            if canonical_edge in existing_edges:
                canonical_index, canonical_metadata = existing_edges[canonical_edge]
                existing_edges[canonical_edge] = (canonical_index, merge_metadata(canonical_metadata, metadata))
                duplicate_edge_indices.append(graph_edge.index)
            else:
                existing_edges[canonical_edge] = (graph_edge.index, metadata)

        new_edges = []
        new_metadata = []
        for logical_edge, metadata in pending_metadata.items():
            if logical_edge in existing_edges:
                canonical_index, existing_metadata = existing_edges[logical_edge]
                existing_edges[logical_edge] = (canonical_index, merge_metadata(existing_metadata, metadata))
            else:
                new_edges.append(logical_edge)
                new_metadata.append(metadata)

        for edge_index, metadata in existing_edges.values():
            for attribute_name in attribute_names:
                self.graph.es[edge_index][attribute_name] = metadata[attribute_name]
        if duplicate_edge_indices:
            self.graph.delete_edges(duplicate_edge_indices)
        if new_edges:
            self.graph.add_edges(new_edges, attributes={name: [metadata[name] for metadata in new_metadata] for name in attribute_names})


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
        if len(self.passage_embeddings) == 0:
            return np.asarray([], dtype=np.int64), np.asarray([], dtype=np.float32)
        query_embedding = self.query_to_embedding['passage'].get(query, None)
        if query_embedding is None:
            query_embedding = self.embedding_model.batch_encode(query,
                                                                instruction=get_query_instruction('query_to_passage'),
                                                                norm=True)
        query_doc_scores = np.dot(self.passage_embeddings, query_embedding.T)
        query_doc_scores = np.squeeze(query_doc_scores) if query_doc_scores.ndim == 2 else query_doc_scores
        query_doc_scores = min_max_normalize(query_doc_scores)

        sorted_doc_ids = np.argsort(query_doc_scores)[::-1]
        sorted_doc_scores = query_doc_scores[sorted_doc_ids.tolist()]
        return sorted_doc_ids, sorted_doc_scores


    def get_top_k_weights(self,
                          link_top_k: int,
                          all_phrase_weights: np.ndarray,
                          linking_score_map: Dict[str, float]) -> Tuple[np.ndarray, Dict[str, float]]:
        """
        This function filters the all_phrase_weights to retain only the weights for the
        top-ranked phrases in terms of the linking_score_map. It also filters linking scores
        to retain only the top `link_top_k` ranked nodes. Non-selected phrases in phrase
        weights are reset to a weight of 0.0.

        Args:
            link_top_k (int): Number of top-ranked nodes to retain in the linking score map.
            all_phrase_weights (np.ndarray): An array representing the phrase weights, indexed
                by phrase ID.
            linking_score_map (Dict[str, float]): A mapping of phrase content to its linking
                score, sorted in descending order of scores.

        Returns:
            Tuple[np.ndarray, Dict[str, float]]: A tuple containing the filtered array
            of all_phrase_weights with unselected weights set to 0.0, and the filtered
            linking_score_map containing only the top `link_top_k` phrases.
        """
        # choose top ranked nodes in linking_score_map
        linking_score_map = dict(sorted(linking_score_map.items(), key=lambda x: x[1], reverse=True)[:link_top_k])

        # only keep the top_k phrases in all_phrase_weights
        top_k_phrases = set(linking_score_map.keys())
        top_k_phrases_keys = set(
            [compute_mdhash_id(content=top_k_phrase, prefix="entity-") for top_k_phrase in top_k_phrases])

        for phrase_key in self.node_name_to_vertex_idx:
            if phrase_key not in top_k_phrases_keys:
                phrase_id = self.node_name_to_vertex_idx.get(phrase_key, None)
                if phrase_id is not None:
                    all_phrase_weights[phrase_id] = 0.0

        return all_phrase_weights, linking_score_map


    def graph_search_with_fact_entities(self, query: str,
                                        link_top_k: int,
                                        query_fact_scores: np.ndarray,
                                        top_k_facts: List[Tuple],
                                        top_k_fact_indices: List[str],
                                        passage_node_weight: float = 0.05) -> Tuple[np.ndarray, np.ndarray]:
        """
        Computes document scores based on fact-based similarity and relevance using personalized
        PageRank (PPR) and dense retrieval models. This function combines the signal from the relevant
        facts identified with passage similarity and graph-based search for enhanced result ranking.

        Parameters:
            query (str): The input query string for which similarity and relevance computations
                need to be performed.
            link_top_k (int): The number of top phrases to include from the linking score map for
                downstream processing.
            query_fact_scores (np.ndarray): An array of scores representing fact-query similarity
                for each of the provided facts.
            top_k_facts (List[Tuple]): A list of top-ranked facts, where each fact is represented
                as a tuple of its subject, predicate, and object.
            top_k_fact_indices (List[str]): Corresponding indices or identifiers for the top-ranked
                facts in the query_fact_scores array.
            passage_node_weight (float): Default weight to scale passage scores in the graph.

        Returns:
            Tuple[np.ndarray, np.ndarray]: A tuple containing two arrays:
                - The first array corresponds to document IDs sorted based on their scores.
                - The second array consists of the PPR scores associated with the sorted document IDs.
        """

        #Assigning phrase weights based on selected facts from previous steps.
        linking_score_map = {}  # from phrase to the average scores of the facts that contain the phrase
        phrase_scores = {}  # store all fact scores for each phrase regardless of whether they exist in the knowledge graph or not
        phrase_weights = np.zeros(len(self.graph.vs['name']))
        passage_weights = np.zeros(len(self.graph.vs['name']))
        number_of_occurs = np.zeros(len(self.graph.vs['name']))

        phrases_and_ids = set()

        for rank, f in enumerate(top_k_facts):
            subject_phrase = f[0].lower()
            predicate_phrase = f[1].lower()
            object_phrase = f[2].lower()
            fact_score = query_fact_scores[
                top_k_fact_indices[rank]] if query_fact_scores.ndim > 0 else query_fact_scores

            for phrase in [subject_phrase, object_phrase]:
                phrase_key = compute_mdhash_id(
                    content=phrase,
                    prefix="entity-"
                )
                phrase_id = self.node_name_to_vertex_idx.get(phrase_key, None)

                if phrase_id is not None:
                    weighted_fact_score = fact_score

                    if len(self.ent_node_to_chunk_ids.get(phrase_key, set())) > 0:
                        weighted_fact_score /= len(self.ent_node_to_chunk_ids[phrase_key])

                    phrase_weights[phrase_id] += weighted_fact_score
                    number_of_occurs[phrase_id] += 1

                    phrases_and_ids.add((phrase, phrase_id))

        phrase_weights = np.divide(phrase_weights, number_of_occurs, out=np.zeros_like(phrase_weights), where=number_of_occurs != 0)

        for phrase, phrase_id in phrases_and_ids:
            if phrase not in phrase_scores:
                phrase_scores[phrase] = []

            phrase_scores[phrase].append(phrase_weights[phrase_id])

        # calculate average fact score for each phrase
        for phrase, scores in phrase_scores.items():
            linking_score_map[phrase] = float(np.mean(scores))

        if link_top_k:
            phrase_weights, linking_score_map = self.get_top_k_weights(link_top_k,
                                                                           phrase_weights,
                                                                           linking_score_map)  # at this stage, the length of linking_scope_map is determined by link_top_k

        #Get passage scores according to chosen dense retrieval model
        dpr_sorted_doc_ids, dpr_sorted_doc_scores = self.dense_passage_retrieval(query)
        normalized_dpr_sorted_scores = min_max_normalize(dpr_sorted_doc_scores)

        for i, dpr_sorted_doc_id in enumerate(dpr_sorted_doc_ids.tolist()):
            passage_node_key = self.passage_node_keys[dpr_sorted_doc_id]
            passage_dpr_score = normalized_dpr_sorted_scores[i]
            passage_node_id = self.node_name_to_vertex_idx[passage_node_key]
            passage_weights[passage_node_id] = passage_dpr_score * passage_node_weight
            passage_node_text = self.chunk_embedding_store.get_row(passage_node_key)["content"]
            linking_score_map[passage_node_text] = passage_dpr_score * passage_node_weight

        #Combining phrase and passage scores into one array for PPR
        node_weights = phrase_weights + passage_weights

        #Recording top 30 facts in linking_score_map
        if len(linking_score_map) > 30:
            linking_score_map = dict(sorted(linking_score_map.items(), key=lambda x: x[1], reverse=True)[:30])

        if sum(node_weights) <= 0:
            raise StateConsistencyError(f'No positive graph seeds were found for facts: {top_k_facts}')

        #Running PPR algorithm based on the passage and phrase weights previously assigned
        ppr_start = time.time()
        ppr_sorted_doc_ids, ppr_sorted_doc_scores = self.run_ppr(node_weights, damping=self.global_config.damping)
        ppr_end = time.time()

        self.ppr_time += (ppr_end - ppr_start)

        if len(ppr_sorted_doc_ids) != len(self.passage_node_idxs):
            raise StateConsistencyError(f"PPR returned {len(ppr_sorted_doc_ids)} documents for a corpus of {len(self.passage_node_idxs)}.")

        return ppr_sorted_doc_ids, ppr_sorted_doc_scores


    def run_ppr(self,
                reset_prob: np.ndarray,
                damping: float =0.5) -> Tuple[np.ndarray, np.ndarray]:
        """
        Runs Personalized PageRank (PPR) on a graph and computes relevance scores for
        nodes corresponding to document passages. The method utilizes a damping
        factor for teleportation during rank computation and can take a reset
        probability array to influence the starting state of the computation.

        Parameters:
            reset_prob (np.ndarray): A 1-dimensional array specifying the reset
                probability distribution for each node. The array must have a size
                equal to the number of nodes in the graph. NaNs or negative values
                within the array are replaced with zeros.
            damping (float): A scalar specifying the damping factor for the
                computation. Defaults to 0.5 if not provided or set to `None`.

        Returns:
            Tuple[np.ndarray, np.ndarray]: A tuple containing two numpy arrays. The
                first array represents the sorted node IDs of document passages based
                on their relevance scores in descending order. The second array
                contains the corresponding relevance scores of each document passage
                in the same order.
        """

        if damping is None: damping = 0.5 # for potential compatibility
        reset_prob = np.where(np.isnan(reset_prob) | (reset_prob < 0), 0, reset_prob)
        pagerank_scores = self.graph.personalized_pagerank(
            vertices=range(len(self.node_name_to_vertex_idx)),
            damping=damping,
            # HippoRAG propagates relevance over the undirected projection of its provenance graph.
            directed=False,
            weights='weight',
            reset=reset_prob,
            implementation='prpack'
        )

        doc_scores = np.array([pagerank_scores[idx] for idx in self.passage_node_idxs])
        sorted_doc_ids = np.argsort(doc_scores)[::-1]
        sorted_doc_scores = doc_scores[sorted_doc_ids.tolist()]

        return sorted_doc_ids, sorted_doc_scores
