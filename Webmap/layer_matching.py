from typing import Dict, List, Optional, Set, Tuple

from layer_analysis import _normalize_text, _title_matches_exact
from layer_replace_builders import _get_layer_id_as_int, _make_hit, _replace_tile_layer, _hard_replace_with_built_block, _soft_replace_feature, _build_expected_new_sublayer_signatures


def _find_descendant_match_for_old_ids(layer_obj: dict, old_ids: Set[str]) -> Optional[str]:
    if not isinstance(layer_obj, dict):
        return None

    item_id = layer_obj.get("itemId")
    service_item_id = layer_obj.get("serviceItemId")
    if item_id in old_ids:
        return item_id
    if service_item_id in old_ids:
        return service_item_id

    children = layer_obj.get("layers")
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, dict):
                continue
            hit = _find_descendant_match_for_old_ids(child, old_ids)
            if hit:
                return hit
    return None


def _collect_descendant_item_ids(layer_obj: dict) -> Set[str]:
    found = set()

    def _walk(obj):
        if not isinstance(obj, dict):
            return

        item_id = obj.get("itemId")
        service_item_id = obj.get("serviceItemId")
        if isinstance(item_id, str) and item_id.strip():
            found.add(item_id)
        if isinstance(service_item_id, str) and service_item_id.strip():
            found.add(service_item_id)

        children = obj.get("layers")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _walk(child)

    _walk(layer_obj)
    return found


def _group_contains_foreign_ids(layer_obj: dict, allowed_old_ids: Set[str]) -> Tuple[bool, Set[str]]:
    all_ids = _collect_descendant_item_ids(layer_obj)
    foreign_ids = {x for x in all_ids if x not in allowed_old_ids}
    return (len(foreign_ids) > 0, foreign_ids)


def _get_direct_matched_old_id(layer_obj: dict, old_ids: Set[str], old_item_titles: Dict[str, str]) -> Optional[str]:
    item_id = layer_obj.get("itemId")
    service_item_id = layer_obj.get("serviceItemId")

    matched_old_id = None
    if item_id in old_ids:
        matched_old_id = item_id
    elif service_item_id in old_ids:
        matched_old_id = service_item_id

    if not matched_old_id:
        return None

    expected_title = old_item_titles.get(matched_old_id)
    if expected_title and not _title_matches_exact(layer_obj.get("title"), expected_title):
        return None
    return matched_old_id


def _analyze_group_replace_candidate(layer_obj: dict, old_ids: Set[str], old_item_titles: Dict[str, str]) -> dict:
    result = {"is_candidate": False, "matched_old_id": None, "has_foreign_ids": False, "foreign_ids": set()}
    if layer_obj.get("layerType") != "GroupLayer":
        return result

    matched_old_id = _find_descendant_match_for_old_ids(layer_obj, old_ids)
    if not matched_old_id:
        return result

    expected_title = old_item_titles.get(matched_old_id)
    if not expected_title:
        return result
    if not _title_matches_exact(layer_obj.get("title"), expected_title):
        return result

    result["is_candidate"] = True
    result["matched_old_id"] = matched_old_id
    has_foreign_ids, foreign_ids = _group_contains_foreign_ids(layer_obj, old_ids)
    result["has_foreign_ids"] = has_foreign_ids
    result["foreign_ids"] = foreign_ids
    return result


def _index_old_group_children_by_title(old_layer_obj: dict) -> Dict[str, List[dict]]:
    by_title = {}
    children = old_layer_obj.get("layers")
    if not isinstance(children, list):
        return by_title

    for child in children:
        if not isinstance(child, dict):
            continue
        title_norm = _normalize_text(child.get("title"))
        if not title_norm:
            continue
        by_title.setdefault(title_norm, []).append(child)
    return by_title


def _index_old_service_children_by_title(old_layer_obj: dict) -> Dict[str, List[dict]]:
    by_title = {}
    children = old_layer_obj.get("layers")
    if not isinstance(children, list):
        return by_title

    for child in children:
        if not isinstance(child, dict):
            continue
        title_norm = _normalize_text(child.get("title") or child.get("name"))
        if not title_norm:
            continue
        by_title.setdefault(title_norm, []).append(child)
    return by_title


def _is_plausible_sublayer_match(old_child: dict, new_sub: dict) -> bool:
    old_layer_id = _get_layer_id_as_int(old_child)
    new_layer_id = new_sub.get("layerId")
    if isinstance(new_layer_id, str) and new_layer_id.isdigit():
        new_layer_id = int(new_layer_id)
    if old_layer_id is None or new_layer_id is None:
        return True
    return old_layer_id == new_layer_id


def _find_matching_old_child_for_new_sub(new_sub: dict, old_by_title: Dict[str, List[dict]]) -> Optional[dict]:
    title_norm = _normalize_text(new_sub.get("title"))
    if not title_norm:
        return None

    candidates = old_by_title.get(title_norm) or []
    if len(candidates) != 1:
        return None

    candidate = candidates[0]
    if not _is_plausible_sublayer_match(candidate, new_sub):
        return None
    return candidate


def _matches_expected_sublayer_signature(layer_obj: dict, expected_signatures: List[Tuple[str, Optional[int]]]) -> bool:
    title_norm = _normalize_text(layer_obj.get("title"))
    if not title_norm:
        return False

    layer_id = _get_layer_id_as_int(layer_obj)
    for exp_title, exp_layer_id in expected_signatures:
        if title_norm != exp_title:
            continue
        if exp_layer_id is None or layer_id is None:
            return True
        if layer_id == exp_layer_id:
            return True
    return False


def _remove_matching_sublayers_outside_path(layers, expected_signatures, skip_exact_path, path="operationalLayers") -> int:
    if not isinstance(layers, list):
        return 0

    removed = 0
    kept = []

    for i, lyr in enumerate(layers):
        current_path = f"{path}[{i}]"

        if not isinstance(lyr, dict):
            kept.append(lyr)
            continue
        if current_path == skip_exact_path:
            kept.append(lyr)
            continue
        if current_path.startswith(skip_exact_path + "."):
            kept.append(lyr)
            continue
        if _matches_expected_sublayer_signature(lyr, expected_signatures):
            removed += 1
            continue

        children = lyr.get("layers")
        if isinstance(children, list):
            removed += _remove_matching_sublayers_outside_path(children, expected_signatures, skip_exact_path, current_path + ".layers")

        kept.append(lyr)

    layers[:] = kept
    return removed


def _walk_and_replace(layers, old_ids, old_item_titles, target_info, context, path="operationalLayers"):
    hits = []
    if not isinstance(layers, list):
        return hits

    for i, lyr in enumerate(layers):
        if not isinstance(lyr, dict):
            continue

        p = f"{path}[{i}]"
        matched_old_id = _get_direct_matched_old_id(lyr, old_ids, old_item_titles)

        if matched_old_id:
            old_type = target_info.get("_classify_layer_obj", None)
            old_type = target_info["_classify_layer_obj"](lyr) if old_type else None
            if old_type is None:
                from layer_analysis import _classify_layer_obj
                old_type = _classify_layer_obj(lyr)

            if target_info["type"] == "tile":
                hits.append(_replace_tile_layer(
                    lyr=lyr,
                    path=p,
                    matched_old_id=matched_old_id,
                    old_type=old_type,
                    old_item_titles=old_item_titles,
                    target_info=target_info,
                    context=context,
                ))
                continue

            use_soft = old_type == "feature" and target_info["type"] == "feature" and target_info["structure_mode"] == "single"
            result = _soft_replace_feature(lyr, matched_old_id, target_info) if use_soft else _hard_replace_with_built_block(lyr, target_info, old_type)

            if result.get("group_replaced"):
                context["group_layer_replaced"] = True

            hits.append(_make_hit(p, matched_old_id, old_item_titles, old_type, target_info["type"], result))
            continue

        group_info = _analyze_group_replace_candidate(lyr, old_ids, old_item_titles)
        if group_info["is_candidate"]:
            if group_info["has_foreign_ids"]:
                context["has_group_conflict"] = True
                continue

            if target_info["type"] == "tile":
                hits.append(_replace_tile_layer(
                    lyr=lyr,
                    path=p,
                    matched_old_id=group_info["matched_old_id"],
                    old_type="group-parent",
                    old_item_titles=old_item_titles,
                    target_info=target_info,
                    context=context,
                ))
                continue

            removed_before_replace = 0
            if target_info["structure_mode"] == "group_children":
                expected_signatures = _build_expected_new_sublayer_signatures(target_info)
                removed_before_replace = _remove_matching_sublayers_outside_path(
                    context["root_operational_layers"], expected_signatures, p, path="operationalLayers"
                )

            result = _hard_replace_with_built_block(lyr, target_info, "group-parent")
            context["group_layer_replaced"] = True
            hits.append(_make_hit(
                p,
                group_info["matched_old_id"],
                old_item_titles,
                "group-parent",
                target_info["type"],
                result,
                removed_before_replace=removed_before_replace,
            ))
            continue

        if lyr.get("layerType") == "GroupLayer" and isinstance(lyr.get("layers"), list):
            hits.extend(_walk_and_replace(lyr["layers"], old_ids, old_item_titles, target_info, context, p + ".layers"))

    return hits
