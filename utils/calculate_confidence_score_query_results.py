def geometric_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    product = 1.0
    for v in values:
        product *= max(v, 1e-9)
    return product ** (1 / len(values))


def graph_hop_score(k: int) -> float:
    """
    confidence_score_graph_hop = max(0, 0.85**(k - 1))
    """
    if k <= 0:
        return 0.0
    return max(0.0, 0.85 ** (k - 1))


def calculate_confidence_score(
    meta_data_query: dict,
    meta_input_query: dict,
    w1: float = 0.45,
    w2: float = 0.20,
    w3: float = 0.35,
) -> dict:
    """
    Calculate confidence score from metadata query results.

    The score is normalized by the sum of weights of available components:
    total_confidence = (score_1 + score_2 + score_3) / (w1_used + w2_used + w3_used)

    This allows handling cases where not all components are present.

    Args:
            meta_data_query: Query metadata containing optional keys:
                    - target_entities: List of target entity matches
                    - attributes: List of attribute matches
                    - related_entities: List of related entity matches
            w1: Weight for target entities score (default: 0.45)
            w2: Weight for attributes score (default: 0.20)
            w3: Weight for related entities score (default: 0.35)
            meta_input_query

    Returns:
            Dict with total_confidence, breakdown, raw scores, and active weights
    """
    scores = []
    weights_used = []
    breakdown = {}
    raw_scores = {}

    # --- S1: target_entities ---
    if "target_entities" in meta_data_query and meta_data_query.get("target_entities"):
        input_target_entities = meta_input_query.get("target_entities", [])
        if not isinstance(input_target_entities, list):
            breakdown["target"] = None
            raw_scores["S1"] = None
        target_scores = [e.get("confident_score_name", 0.0) for e in meta_data_query.get("target_entities", [])]
        S1 = max(target_scores) if target_scores else 0.0
        score_1 = min(w1, S1 * w1)

        scores.append(score_1)
        weights_used.append(w1)
        breakdown["target"] = round(score_1, 4)
        raw_scores["S1"] = round(S1, 4)
    else:
        breakdown["target"] = None
        raw_scores["S1"] = None

    # --- S2: attributes ---
    attributes_data = meta_data_query.get("attributes", {})
    target_attrs = attributes_data.get("target_attribute", [])
    related_attrs = attributes_data.get("related_entities_attribute", [])

    has_input_attrs = (
        len(meta_input_query.get("target_attribute", [])) > 0
        or len(meta_input_query.get("related_entities_attribute", [])) > 0
    )
    has_output_attrs = len(target_attrs) > 0 or len(related_attrs) > 0

    if has_output_attrs or has_input_attrs:
        attr_scores = []
        for a in target_attrs:
            attr_scores.append(a.get("confidence_score_attribute", 0.0))
        for a in related_attrs:
            attr_scores.append(a.get("confidence_score_attribute", 0.0))

        S2 = geometric_mean(attr_scores) if attr_scores else 0.0
        score_2 = min(w2, S2 * w2)

        scores.append(score_2)
        weights_used.append(w2)
        breakdown["attribute"] = round(score_2, 4)
        raw_scores["S2"] = round(S2, 4)
    else:
        breakdown["attribute"] = None
        raw_scores["S2"] = None

    # --- S3: related_entities ---
    if (
        "related_entities" in meta_data_query
        and meta_data_query.get("related_entities")
        or (
            len(meta_input_query.get("related_entities_type", [])) > 0
            or len(meta_input_query.get("related_entities", [])) > 0
        )
    ):
        related_effective_scores = []
        list_related = meta_data_query.get("related_entities", [])
        if list_related:
            breakdown["related"] = min(w3, 0.5 * w3)
            raw_scores["S3"] = min(w3, 0.5 * w3)
        for r in list_related:
            # Get similarity score
            similarity = r.get("confident_score_name", 1.0)

            # Calculate graph hop score
            hop = r.get("graph_hop", 0)
            graph_score = graph_hop_score(hop)

            # Calculate effective score
            effective_score = similarity * graph_score
            related_effective_scores.append(effective_score)

        S3 = geometric_mean(related_effective_scores)
        score_3 = min(w3, S3 * w3)

        scores.append(score_3)
        weights_used.append(w3)
        breakdown["related"] = round(score_3, 4)
        raw_scores["S3"] = round(S3, 4)
    else:
        breakdown["related"] = None
        raw_scores["S3"] = None

    # --- Calculate Total Confidence ---
    # Normalize by sum of weights used
    total_weight = sum(weights_used)

    if total_weight > 0:
        total = sum(scores) / total_weight
    else:
        total = 0.0

    return {
        "total_confidence": round(total, 4),
        "breakdown": breakdown,
        "raw": raw_scores,
        "weights_used": {
            "w1": w1 if "target" in breakdown and breakdown["target"] is not None else 0,
            "w2": w2 if "attribute" in breakdown and breakdown["attribute"] is not None else 0,
            "w3": w3 if "related" in breakdown and breakdown["related"] is not None else 0,
            "total": round(total_weight, 4),
        },
    }
