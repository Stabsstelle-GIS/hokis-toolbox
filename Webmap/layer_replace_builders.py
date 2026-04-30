import json
import re
from typing import Dict, List, Optional, Tuple

UPDATE_TITLE_TO_NEW = True

SAFE_KEYS = {"id", "visibility", "opacity", "minScale", "maxScale"}

TYPE_KEYS = {
    "feature": {"showLegend", "disablePopup"},
    "mapimage": {"showLegend", "disablePopup", "visibleLayers"},
    "wms": {"showLegend", "featureInfoFormat", "featureInfoUrl", "mapUrl", "spatialReferences", "layers", "visibleLayers", "version"},
    "vectortile": {"styleUrl", "blendMode", "isReference"},
    "tile": {"showLegend"},
    "other": set(),
}

NEVER_COPY_KEYS = {"featureCollection"}


def _msg(message_func, text: str):
    if message_func:
        message_func(text)
    else:
        print(text)


def _warn(warning_func, text: str):
    if warning_func:
        warning_func(text)
    else:
        print(f"WARNING: {text}")


def _add_runtime_issue(runtime_issues: List[str], message: str):
    if message not in runtime_issues:
        runtime_issues.append(message)


def _json_clone(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False))


def _update_webmap_via_rest(gis, wm_item, data: dict) -> bool:
    owner = getattr(wm_item, "owner", None)
    item_id = getattr(wm_item, "id", None)

    if not owner or not item_id:
        raise RuntimeError("REST-Update nicht moeglich: owner oder item_id fehlt.")

    resturl = getattr(gis._portal, "resturl", None)
    if not resturl:
        raise RuntimeError("REST-Update nicht moeglich: portal.resturl fehlt.")

    update_url = f"{resturl}content/users/{owner}/items/{item_id}/update"
    payload = {"f": "json", "text": json.dumps(data, ensure_ascii=False)}
    res = gis._con.post(update_url, payload)
    if not isinstance(res, dict):
        return False
    return bool(res.get("success"))


def _collect_transferable_props(old_layer_obj: dict, old_type: str, new_type: str) -> dict:
    allowed = set(SAFE_KEYS)
    if old_type == new_type:
        allowed |= TYPE_KEYS.get(old_type, set())
    allowed |= TYPE_KEYS.get(new_type, set())
    allowed -= NEVER_COPY_KEYS
    out = {}
    for k in allowed:
        if k in old_layer_obj:
            out[k] = _json_clone(old_layer_obj[k])
    return out


def _extract_sublayer_suffix(layer_obj: dict) -> str:
    url = layer_obj.get("url")
    if isinstance(url, str):
        m = re.search(r"/(\d+)$", url)
        if m:
            return f"/{m.group(1)}"

    lid = layer_obj.get("layerId")
    if isinstance(lid, int):
        return f"/{lid}"
    if isinstance(lid, str) and lid.isdigit():
        return f"/{lid}"
    return ""


def _get_layer_id_as_int(layer_obj: dict):
    if not isinstance(layer_obj, dict):
        return None

    lid = layer_obj.get("layerId")
    if isinstance(lid, int):
        return lid
    if isinstance(lid, str) and lid.isdigit():
        return int(lid)

    lid = layer_obj.get("id")
    if isinstance(lid, int):
        return lid
    if isinstance(lid, str) and lid.isdigit():
        return int(lid)
    return None


def _copy_group_parent_safe_props(old_layer_obj: dict, old_type: str) -> dict:
    out = _collect_transferable_props(old_layer_obj, old_type, "other")
    out.pop("minScale", None)
    out.pop("maxScale", None)
    return out


def _build_clean_base_props_for_service_target(old_layer_obj: dict, new_title: Optional[str], fallback_id: str) -> dict:
    out = {}
    out["id"] = _json_clone(old_layer_obj["id"]) if old_layer_obj.get("id") is not None else fallback_id
    out["title"] = new_title if new_title else old_layer_obj.get("title")
    out["visibility"] = _json_clone(old_layer_obj.get("visibility", True))
    out["opacity"] = _json_clone(old_layer_obj.get("opacity", 1))
    if "minScale" in old_layer_obj:
        out["minScale"] = _json_clone(old_layer_obj["minScale"])
    if "maxScale" in old_layer_obj:
        out["maxScale"] = _json_clone(old_layer_obj["maxScale"])
    return out


def _make_hit(path, matched_old_id, old_item_titles, old_type, new_type, result, **extra):
    return {
        "path": path,
        "matched_old_id": matched_old_id,
        "old_item_title_filter": old_item_titles.get(matched_old_id),
        "old_type": old_type,
        "new_type": new_type,
        **extra,
        **result,
    }


def _build_new_url(old_layer_obj: dict, target_info: dict) -> Optional[str]:
    new_item_url = target_info["url"]
    if not new_item_url:
        return None
    if target_info["type"] in {"mapimage", "tile", "wms"}:
        return re.sub(r"/\d+$", "", new_item_url.rstrip("/"))
    if re.search(r"/\d+$", new_item_url):
        return new_item_url
    suffix = _extract_sublayer_suffix(old_layer_obj)
    return new_item_url.rstrip("/") + suffix if suffix else new_item_url


def _build_expected_new_sublayer_signatures(target_info: dict) -> List[Tuple[str, Optional[int]]]:
    from layer_analysis import _normalize_text
    return [(_normalize_text(sub.get("title")), sub.get("layerId")) for sub in target_info.get("sublayers", [])]


def _build_minimal_tile_block(old_layer_obj: dict, target_info: dict) -> dict:
    if not target_info.get("url"):
        raise RuntimeError("Tile-Ziel hat keine URL.")

    tile_url = re.sub(r"/\d+$", "", target_info["url"].rstrip("/"))
    fresh = {
        "itemId": target_info["itemId"],
        "url": tile_url,
        "layerType": "ArcGISTiledMapServiceLayer",
        "visibility": _json_clone(old_layer_obj.get("visibility", True)),
    }

    if UPDATE_TITLE_TO_NEW and target_info.get("title"):
        fresh["title"] = target_info["title"]
    elif old_layer_obj.get("title"):
        fresh["title"] = _json_clone(old_layer_obj["title"])

    allowed = (set(SAFE_KEYS) - {"id"}) | TYPE_KEYS.get("tile", set())
    allowed -= NEVER_COPY_KEYS

    for k in allowed:
        if k in old_layer_obj:
            fresh[k] = _json_clone(old_layer_obj[k])

    for bad_key in ("featureCollection", "layers", "visibleLayers", "layerDefinition", "popupInfo", "serviceItemId", "disablePopup", "visibilityMode"):
        fresh.pop(bad_key, None)

    return fresh


def _replace_tile_layer(lyr: dict, path: str, matched_old_id: str, old_type: str, old_item_titles: Dict[str, str], target_info: dict, context: dict):
    before = _json_clone(lyr)
    fresh = _build_minimal_tile_block(before, target_info)
    lyr.clear()
    lyr.update(fresh)
    context["tile_keep_path"] = path
    return _make_hit(
        path,
        matched_old_id,
        old_item_titles,
        old_type,
        target_info["type"],
        {
            "mode": f"TILE_MINIMAL_REPLACE({old_type}->tile)",
            "changed": True,
            "before_title": before.get("title"),
            "after_title": lyr.get("title"),
            "before_url": before.get("url"),
            "after_url": lyr.get("url"),
            "transferred_keys": None,
            "dropped_keys": sorted([k for k in before.keys() if k not in lyr]),
            "group_replaced": False,
            "final_block": _json_clone(lyr),
        },
    )


def _dedupe_target_tile_layers(layers, target_item_id: str, keep_path: Optional[str] = None, path="operationalLayers") -> int:
    if not isinstance(layers, list):
        return 0

    removed = 0
    kept = []

    for i, lyr in enumerate(layers):
        current_path = f"{path}[{i}]"

        if not isinstance(lyr, dict):
            kept.append(lyr)
            continue

        is_target = lyr.get("itemId") == target_item_id
        if is_target and current_path != keep_path:
            removed += 1
            continue

        children = lyr.get("layers")
        if isinstance(children, list):
            removed += _dedupe_target_tile_layers(children, target_item_id=target_item_id, keep_path=keep_path, path=current_path + ".layers")

        kept.append(lyr)

    layers[:] = kept
    return removed


def _collect_matching_target_layers(layers, target_item_id: str, out: Optional[List[dict]] = None) -> List[dict]:
    if out is None:
        out = []
    if not isinstance(layers, list):
        return out

    for lyr in layers:
        if not isinstance(lyr, dict):
            continue
        if lyr.get("itemId") == target_item_id:
            out.append(lyr)
        children = lyr.get("layers")
        if isinstance(children, list):
            _collect_matching_target_layers(children, target_item_id, out)
    return out


def _validate_final_tile_targets(data: dict, target_info: dict) -> Tuple[bool, List[str]]:
    messages = []
    matches = _collect_matching_target_layers(data.get("operationalLayers", []), target_info["itemId"])

    if len(matches) != 1:
        messages.append(f"Finale Validierung: erwartet genau 1 Ziel-Layer fuer Tile, gefunden: {len(matches)}")
        return False, messages

    lyr = matches[0]
    if lyr.get("layerType") != "ArcGISTiledMapServiceLayer":
        messages.append(f"Finale Validierung: falscher layerType fuer Tile-Ziel: {lyr.get('layerType')}")
        return False, messages

    return True, messages


def _build_structured_feature_child_layer(old_child: Optional[dict], sub: dict, target_info: dict) -> dict:
    from layer_analysis import _classify_layer_obj

    target_item_id = target_info["itemId"]
    transfer_props = _collect_transferable_props(old_child, _classify_layer_obj(old_child), "feature") if old_child else {}

    child = {}
    child.update(transfer_props)
    child["id"] = f"{target_item_id}_{sub['layerId']}"
    child["title"] = sub["title"]
    child["url"] = sub["url"]
    child["itemId"] = target_item_id
    child["serviceItemId"] = target_item_id
    child["layerId"] = sub["layerId"]
    child["layerType"] = "ArcGISFeatureLayer"

    for k in NEVER_COPY_KEYS:
        child.pop(k, None)
    return child


def _build_feature_group_children_with_transfer(old_layer_obj: dict, target_info: dict):
    from layer_matching import _index_old_group_children_by_title, _find_matching_old_child_for_new_sub

    children = []
    old_by_title = _index_old_group_children_by_title(old_layer_obj)

    for sub in target_info["sublayers"]:
        old_child = _find_matching_old_child_for_new_sub(sub, old_by_title)
        child = _build_structured_feature_child_layer(old_child, sub, target_info) if old_child else _build_structured_feature_child_layer(None, sub, target_info)
        children.append(child)

    children.reverse()
    return children


def _build_feature_group_block(old_layer_obj: dict, target_info: dict, old_type: str) -> dict:
    fresh = {}
    fresh.update(_copy_group_parent_safe_props(old_layer_obj, old_type))
    fresh["layerType"] = "GroupLayer"
    fresh["title"] = target_info["title"] if (UPDATE_TITLE_TO_NEW and target_info["title"]) else old_layer_obj.get("title")
    fresh["layers"] = _build_feature_group_children_with_transfer(old_layer_obj, target_info)
    fresh["visibilityMode"] = "independent"
    for k in NEVER_COPY_KEYS:
        fresh.pop(k, None)
    return fresh


def _build_single_layer_block(old_layer_obj: dict, target_info: dict, old_type: str) -> dict:
    new_type = target_info["type"]
    new_id = target_info["itemId"]
    new_url = _build_new_url(old_layer_obj, target_info)
    title = target_info["title"] if (UPDATE_TITLE_TO_NEW and target_info["title"]) else old_layer_obj.get("title")

    if not new_url and new_type != "vectortile":
        raise RuntimeError("Neue URL fehlt.")
    if new_type == "tile":
        raise RuntimeError("Tile wird im Sonderweg behandelt und darf hier nicht landen.")
    if new_type == "wms":
        raise RuntimeError("WMS mit Unterlayern wird ueber Sonderweg gebaut und darf hier nicht landen.")

    if new_type == "mapimage":
        fresh = _build_clean_base_props_for_service_target(old_layer_obj, title, new_id)
        fresh["layerType"] = "ArcGISMapServiceLayer"
        fresh["itemId"] = new_id
        if new_url:
            fresh["url"] = new_url
        return fresh

    fresh = _collect_transferable_props(old_layer_obj, old_type, new_type)
    fresh["title"] = title
    for k in NEVER_COPY_KEYS:
        fresh.pop(k, None)

    if new_type == "feature":
        fresh["layerType"] = "ArcGISFeatureLayer"
    elif new_type == "vectortile":
        fresh["layerType"] = "VectorTileLayer"
        if target_info.get("styleUrl"):
            fresh["styleUrl"] = target_info["styleUrl"]
    else:
        raise RuntimeError(f"Zieltyp '{new_type}' wird nicht unterstuetzt.")

    fresh["itemId"] = new_id
    fresh["serviceItemId"] = new_id
    if new_url:
        fresh["url"] = new_url
    return fresh


def _build_mapimage_service_with_layers_block(old_layer_obj: dict, target_info: dict, old_type: str) -> dict:
    from layer_matching import _index_old_service_children_by_title, _find_matching_old_child_for_new_sub

    new_url = _build_new_url(old_layer_obj, target_info)
    if not new_url:
        raise RuntimeError("Neue URL fehlt.")

    fresh = _build_clean_base_props_for_service_target(
        old_layer_obj=old_layer_obj,
        new_title=target_info["title"] if (UPDATE_TITLE_TO_NEW and target_info["title"]) else old_layer_obj.get("title"),
        fallback_id=target_info["itemId"],
    )

    fresh["layerType"] = "ArcGISMapServiceLayer"
    fresh["itemId"] = target_info["itemId"]
    fresh["url"] = new_url

    old_by_title = _index_old_service_children_by_title(old_layer_obj)
    visible_layer_ids = []

    for sub in target_info["sublayers"]:
        old_child = _find_matching_old_child_for_new_sub(sub, old_by_title)
        if isinstance(old_child, dict):
            if old_child.get("visibility") is True:
                visible_layer_ids.append(sub["layerId"])
        else:
            if sub.get("defaultVisibility") is True:
                visible_layer_ids.append(sub["layerId"])

    if visible_layer_ids:
        fresh["visibleLayers"] = visible_layer_ids
    return fresh


def _build_wms_with_layers_block(old_layer_obj: dict, target_info: dict, old_type: str) -> dict:
    from layer_analysis import _strip_query
    from layer_matching import _index_old_service_children_by_title, _find_matching_old_child_for_new_sub

    fresh = {}
    fresh.update(_collect_transferable_props(old_layer_obj, old_type, "wms"))
    new_url = _build_new_url(old_layer_obj, target_info)
    if not new_url:
        raise RuntimeError("Neue URL fehlt.")

    fresh["title"] = target_info["title"] if (UPDATE_TITLE_TO_NEW and target_info["title"]) else old_layer_obj.get("title")
    fresh["layerType"] = "WMS"
    fresh["itemId"] = target_info["itemId"]
    fresh["url"] = new_url

    map_url = _strip_query(target_info.get("mapUrl") or target_info.get("url"))
    feature_info_url = _strip_query(target_info.get("featureInfoUrl") or target_info.get("url"))
    if map_url:
        fresh["mapUrl"] = map_url
    if feature_info_url:
        fresh["featureInfoUrl"] = feature_info_url
    if target_info.get("featureInfoFormat"):
        fresh["featureInfoFormat"] = target_info["featureInfoFormat"]
    if target_info.get("spatialReferences"):
        fresh["spatialReferences"] = target_info["spatialReferences"]
    if target_info.get("version"):
        fresh["version"] = target_info["version"]

    wms_layers = []
    for sub in target_info["sublayers"]:
        sub_name = sub.get("name")
        if not sub_name:
            continue
        wms_layers.append({
            "legendUrl": sub.get("legendUrl"),
            "name": sub_name,
            "showPopup": bool(sub.get("showPopup", False)),
            "queryable": bool(sub.get("queryable", True)),
            "title": sub.get("title") or sub_name,
        })

    if not wms_layers:
        raise RuntimeError("WMS-Ziel hat keine gueltigen Sublayer.")

    fresh["layers"] = wms_layers
    old_by_title = _index_old_service_children_by_title(old_layer_obj)
    visible_names = []

    for sub in target_info["sublayers"]:
        old_child = _find_matching_old_child_for_new_sub(sub, old_by_title)
        if isinstance(old_child, dict) and old_child.get("visibility") is True:
            visible_names.append(sub["name"])

    fresh["visibleLayers"] = visible_names if visible_names else [lyr["name"] for lyr in wms_layers]
    for k in NEVER_COPY_KEYS:
        fresh.pop(k, None)
    fresh.pop("serviceItemId", None)
    return fresh


def _build_replacement_block(old_layer_obj: dict, target_info: dict, old_type: str) -> Tuple[dict, str]:
    if target_info["structure_mode"] == "group_children":
        return _build_feature_group_block(old_layer_obj, target_info, old_type), f"GROUP_CHILDREN({old_type}->{target_info['type']})"
    if target_info["structure_mode"] == "service_with_layers":
        return _build_mapimage_service_with_layers_block(old_layer_obj, target_info, old_type), f"SERVICE_WITH_LAYERS({old_type}->{target_info['type']})"
    if target_info["structure_mode"] == "wms_with_layers":
        return _build_wms_with_layers_block(old_layer_obj, target_info, old_type), f"WMS_WITH_LAYERS({old_type}->{target_info['type']})"
    return _build_single_layer_block(old_layer_obj, target_info, old_type), f"SINGLE_BLOCK({old_type}->{target_info['type']})"


def _soft_replace_feature(layer_obj: dict, matched_old_id: str, target_info: dict):
    before = _json_clone(layer_obj)
    changed = False
    new_url = _build_new_url(layer_obj, target_info)

    if layer_obj.get("itemId") == matched_old_id:
        layer_obj["itemId"] = target_info["itemId"]
        changed = True
    if layer_obj.get("serviceItemId") == matched_old_id:
        layer_obj["serviceItemId"] = target_info["itemId"]
        changed = True
    if "featureCollection" in layer_obj:
        layer_obj.pop("featureCollection", None)
        changed = True
    if new_url and layer_obj.get("url") != new_url:
        layer_obj["url"] = new_url
        changed = True
    if UPDATE_TITLE_TO_NEW and target_info["title"] and layer_obj.get("title") != target_info["title"]:
        layer_obj["title"] = target_info["title"]
        changed = True

    after = _json_clone(layer_obj)
    return {
        "mode": "SOFT(feature)",
        "changed": changed,
        "before_title": before.get("title"),
        "after_title": after.get("title"),
        "before_url": before.get("url"),
        "after_url": after.get("url"),
        "transferred_keys": None,
        "dropped_keys": None,
        "group_replaced": False,
        "final_block": after,
    }


def _hard_replace_with_built_block(layer_obj: dict, target_info: dict, old_type: str):
    before = _json_clone(layer_obj)
    fresh, mode = _build_replacement_block(before, target_info, old_type)

    dropped_keys = sorted([k for k in before.keys() if k not in fresh])
    transferred_keys = sorted([k for k in fresh.keys() if k in before and k not in {"title", "url", "itemId", "serviceItemId", "layerType", "layers", "visibilityMode"}])

    layer_obj.clear()
    layer_obj.update(fresh)
    after = _json_clone(layer_obj)

    return {
        "mode": mode,
        "changed": True,
        "before_title": before.get("title"),
        "after_title": layer_obj.get("title"),
        "before_url": before.get("url"),
        "after_url": layer_obj.get("url"),
        "transferred_keys": transferred_keys,
        "dropped_keys": dropped_keys,
        "group_replaced": (old_type == "group-parent" or target_info["structure_mode"] == "group_children"),
        "final_block": after,
    }