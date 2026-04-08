import json
import re
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from typing import Dict, List, Optional, Set, Tuple

import arcpy
import arcgis
import requests
from arcgis.gis import GIS
from arcgis.features import FeatureLayerCollection


# =========================================================
# Feste Konfiguration
# =========================================================
UPDATE_TITLE_TO_NEW = True
DEBUG_DUMP_FINAL_BLOCKS = True

# Wenn wm.update(...) scheitert, optional REST-Fallback versuchen
USE_REST_UPDATE_FALLBACK = True

SAFE_KEYS = {
    "id",
    "visibility",
    "opacity",
    "minScale",
    "maxScale",
}

SAME_TYPE_KEYS = {
    "feature": {
        "showLegend",
        "disablePopup",
    },
    "mapimage": {
        "showLegend",
        "disablePopup",
        "visibleLayers",
    },
    "wms": {
        "featureInfoFormat",
        "featureInfoUrl",
        "mapUrl",
        "spatialReferences",
        "layers",
        "visibleLayers",
        "version",
    },
    "vectortile": {
        "styleUrl",
        "blendMode",
        "isReference",
    },
    "tile": {
        "showLegend",
    },
    "other": set(),
}

TARGET_TYPE_KEYS = {
    "feature": {
        "showLegend",
        "disablePopup",
    },
    "mapimage": {
        "showLegend",
        "disablePopup",
        "visibleLayers",
    },
    "wms": {
        "visibleLayers",
        "layers",
        "mapUrl",
        "featureInfoUrl",
        "featureInfoFormat",
        "spatialReferences",
        "version",
    },
    "vectortile": {
        "styleUrl",
        "blendMode",
        "isReference",
    },
    "tile": {
        "showLegend",
    },
    "other": set(),
}

NEVER_COPY_KEYS = {"featureCollection"}


# =========================================================
# Meldungsfunktionen
# =========================================================
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


# =========================================================
# Laufzeit-Hinweise / Probleme sammeln
# =========================================================
def _add_runtime_issue(runtime_issues: List[str], message: str):
    if message not in runtime_issues:
        runtime_issues.append(message)


# =========================================================
# JSON-sicher klonen
# =========================================================
def _json_clone(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False))


# =========================================================
# REST-Fallback fuer Item-Update
# =========================================================
def _update_webmap_via_rest(gis, wm_item, data: dict) -> bool:
    owner = getattr(wm_item, "owner", None)
    item_id = getattr(wm_item, "id", None)

    if not owner or not item_id:
        raise RuntimeError("REST-Update nicht moeglich: owner oder item_id fehlt.")

    resturl = getattr(gis._portal, "resturl", None)
    if not resturl:
        raise RuntimeError("REST-Update nicht moeglich: portal.resturl fehlt.")

    update_url = f"{resturl}content/users/{owner}/items/{item_id}/update"
    payload = {
        "f": "json",
        "text": json.dumps(data, ensure_ascii=False),
    }

    res = gis._con.post(update_url, payload)
    if not isinstance(res, dict):
        return False

    return bool(res.get("success"))


# =========================================================
# Kleine Hilfsfunktionen
# =========================================================
def _normalize_text(value) -> str:
    return str(value).strip().casefold() if value is not None else ""


def _title_matches_exact(a, b) -> bool:
    return _normalize_text(a) == _normalize_text(b)


def _strip_query(url: Optional[str]) -> Optional[str]:
    if not isinstance(url, str):
        return url
    return url.split("?", 1)[0]


def _collect_transferable_props(old_layer_obj: dict, old_type: str, new_type: str) -> dict:
    allowed = set(SAFE_KEYS)

    if old_type == new_type:
        allowed |= SAME_TYPE_KEYS.get(old_type, set())

    allowed |= TARGET_TYPE_KEYS.get(new_type, set())
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


def _safe_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _build_clean_base_props_for_service_target(old_layer_obj: dict, new_title: Optional[str], fallback_id: str) -> dict:
    out = {}

    if old_layer_obj.get("id") is not None:
        out["id"] = _json_clone(old_layer_obj["id"])
    else:
        out["id"] = fallback_id

    out["title"] = new_title if new_title else old_layer_obj.get("title")
    out["visibility"] = _json_clone(old_layer_obj.get("visibility", True))
    out["opacity"] = _json_clone(old_layer_obj.get("opacity", 1))

    if "minScale" in old_layer_obj:
        out["minScale"] = _json_clone(old_layer_obj["minScale"])
    if "maxScale" in old_layer_obj:
        out["maxScale"] = _json_clone(old_layer_obj["maxScale"])

    return out


# =========================================================
# WMS XML / Capabilities
# =========================================================
def _strip_xml_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _build_wms_capabilities_url(url: str) -> str:
    base = url.strip()
    sep = "&" if "?" in base else "?"
    return base + sep + urlencode({
        "service": "WMS",
        "request": "GetCapabilities",
    })


def _find_xml_child(elem, local_name: str):
    for child in list(elem):
        if _strip_xml_ns(child.tag) == local_name:
            return child
    return None


def _find_xml_child_text(elem, local_name: str) -> Optional[str]:
    child = _find_xml_child(elem, local_name)
    if child is not None and child.text:
        text = child.text.strip()
        if text:
            return text
    return None


def _extract_online_resource_href(elem) -> Optional[str]:
    if elem is None:
        return None

    for k, v in elem.attrib.items():
        if k.lower().endswith("href") and isinstance(v, str) and v.strip():
            return v.strip()

    return None


def _extract_legend_url_from_layer(layer_elem) -> Optional[str]:
    for style_elem in list(layer_elem):
        if _strip_xml_ns(style_elem.tag) != "Style":
            continue

        legend_url_elem = _find_xml_child(style_elem, "LegendURL")
        if legend_url_elem is None:
            continue

        online_resource_elem = _find_xml_child(legend_url_elem, "OnlineResource")
        href = _extract_online_resource_href(online_resource_elem)
        if href:
            return href

    return None


def _parse_wms_layers_from_capabilities_xml(xml_text: str) -> List[dict]:
    out = []

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    def _walk_layer(layer_elem):
        name = _find_xml_child_text(layer_elem, "Name")
        title = _find_xml_child_text(layer_elem, "Title")

        queryable_raw = layer_elem.attrib.get("queryable")
        queryable = str(queryable_raw).strip() in {"1", "true", "True"}

        legend_url = _extract_legend_url_from_layer(layer_elem)

        if name:
            out.append({
                "layerId": len(out),
                "name": name,
                "title": title or name,
                "url": None,
                "queryable": queryable,
                "showPopup": False,
                "legendUrl": legend_url,
            })

        for child in list(layer_elem):
            if _strip_xml_ns(child.tag) == "Layer":
                _walk_layer(child)

    for elem in root.iter():
        if _strip_xml_ns(elem.tag) == "Capability":
            for child in list(elem):
                if _strip_xml_ns(child.tag) == "Layer":
                    _walk_layer(child)
            break

    return out


# =========================================================
# Item / Typ / URL
# =========================================================
def _get_item_url(item):
    url = getattr(item, "url", None)
    if isinstance(url, str) and url.strip():
        return url

    try:
        props = getattr(item, "properties", None)
        if props:
            url = getattr(props, "url", None)
            if isinstance(url, str) and url.strip():
                return url
    except Exception:
        pass

    try:
        data = item.get_data()
        if isinstance(data, dict):
            for key in ("url", "serviceUrl"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    return val
    except Exception:
        pass

    try:
        flc = FeatureLayerCollection.fromitem(item)
        url = getattr(flc, "url", None)
        if isinstance(url, str) and url.strip():
            return url
    except Exception:
        pass

    return None


def _get_vector_tile_style_url(item):
    try:
        data = item.get_data()
        if isinstance(data, dict):
            style_url = data.get("styleUrl")
            if isinstance(style_url, str) and style_url.strip():
                return style_url
    except Exception:
        pass

    item_url = _get_item_url(item)
    if isinstance(item_url, str) and item_url.strip():
        return item_url.rstrip("/") + "/resources/styles/root.json"

    return None


def _get_feature_sublayers(item):
    try:
        flc = FeatureLayerCollection.fromitem(item)
        service_url = getattr(flc, "url", None) or _get_item_url(item)
        layers = getattr(flc, "layers", None)

        if isinstance(layers, list) and layers:
            out = []
            for lyr in layers:
                try:
                    props = getattr(lyr, "properties", None)
                    layer_id = getattr(props, "id", None)
                    name = getattr(props, "name", None)
                    if layer_id is None:
                        continue

                    layer_id = int(layer_id)

                    out.append({
                        "layerId": layer_id,
                        "title": str(name) if name else f"Layer {layer_id}",
                        "name": str(name) if name else f"Layer {layer_id}",
                        "url": f"{service_url.rstrip('/')}/{layer_id}" if service_url else None,
                    })
                except Exception:
                    continue

            if out:
                return out
    except Exception:
        pass

    return []


def _fetch_service_json(gis, service_url: str):
    if not gis or not service_url:
        return None

    try:
        return gis._con.get(service_url, {"f": "json"})
    except Exception:
        return None


def _detect_mapserver_mode(gis, service_url: Optional[str]) -> str:
    if not gis or not service_url:
        return "other"

    if "/mapserver" not in service_url.lower():
        return "other"

    base_url = re.sub(r"/\d+$", "", service_url.rstrip("/"))
    info = _fetch_service_json(gis, base_url)
    if not isinstance(info, dict):
        return "other"

    if info.get("singleFusedMapCache") is True:
        return "tile"

    if isinstance(info.get("tileInfo"), dict):
        return "tile"

    return "mapimage"


def _get_service_sublayers(gis, service_url: str):
    if not service_url:
        return []

    base_url = re.sub(r"/\d+$", "", service_url.rstrip("/"))
    info = _fetch_service_json(gis, base_url)
    if not isinstance(info, dict):
        return []

    layers = info.get("layers")
    if not isinstance(layers, list) or not layers:
        return []

    out = []
    for lyr in layers:
        if not isinstance(lyr, dict):
            continue

        layer_id = lyr.get("id")
        if layer_id is None:
            continue

        try:
            layer_id = int(layer_id)
        except Exception:
            continue

        title = lyr.get("name") or lyr.get("title") or f"Layer {layer_id}"

        sub_ids = lyr.get("subLayerIds")
        if isinstance(sub_ids, list):
            clean_sub_ids = []
            for sid in sub_ids:
                sid_int = _safe_int(sid)
                if sid_int is not None:
                    clean_sub_ids.append(sid_int)
        else:
            clean_sub_ids = None

        parent_layer_id = _safe_int(lyr.get("parentLayerId"))

        out.append({
            "layerId": layer_id,
            "title": str(title),
            "name": str(lyr.get("name") or title),
            "url": f"{base_url}/{layer_id}",
            "defaultVisibility": bool(lyr.get("defaultVisibility", False)),
            "parentLayerId": parent_layer_id,
            "subLayerIds": clean_sub_ids,
        })

    return out


def _get_wms_item_metadata(item) -> dict:
    meta = {
        "url": None,
        "mapUrl": None,
        "featureInfoUrl": None,
        "featureInfoFormat": None,
        "spatialReferences": None,
        "version": None,
        "format": None,
    }

    try:
        data = item.get_data()
        if isinstance(data, dict):
            for key in ("url", "mapUrl", "featureInfoUrl", "featureInfoFormat", "version", "format"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    meta[key] = val.strip()

            srefs = data.get("spatialReferences")
            if isinstance(srefs, list) and srefs:
                meta["spatialReferences"] = srefs
    except Exception:
        pass

    try:
        props = getattr(item, "properties", None)
        if props:
            for key in ("url", "mapUrl", "featureInfoUrl", "featureInfoFormat", "version", "format"):
                val = getattr(props, key, None)
                if isinstance(val, str) and val.strip() and not meta.get(key):
                    meta[key] = val.strip()

            srefs = getattr(props, "spatialReferences", None)
            if isinstance(srefs, list) and srefs and not meta.get("spatialReferences"):
                meta["spatialReferences"] = srefs
    except Exception:
        pass

    if not meta["url"]:
        item_url = _get_item_url(item)
        if isinstance(item_url, str) and item_url.strip():
            meta["url"] = item_url.strip()

    if not meta["mapUrl"] and meta["url"]:
        meta["mapUrl"] = _strip_query(meta["url"])

    if not meta["featureInfoUrl"] and meta["url"]:
        meta["featureInfoUrl"] = _strip_query(meta["url"])

    if not meta["version"]:
        meta["version"] = "1.3.0"

    if not meta["format"]:
        meta["format"] = "png"

    return meta


def _get_wms_sublayers_from_item(item, gis=None):
    candidates = []

    try:
        data = item.get_data()
        if isinstance(data, dict):
            for key in ("layers", "visibleLayers"):
                val = data.get(key)
                if isinstance(val, list):
                    candidates.extend(val)
    except Exception:
        pass

    try:
        props = getattr(item, "properties", None)
        if props:
            for key in ("layers", "visibleLayers"):
                val = getattr(props, key, None)
                if isinstance(val, list):
                    candidates.extend(val)
    except Exception:
        pass

    out = []
    seen = set()

    for i, lyr in enumerate(candidates):
        if isinstance(lyr, dict):
            layer_name = lyr.get("name") or lyr.get("id") or lyr.get("title")
            title = lyr.get("title") or lyr.get("name")
            if not layer_name:
                continue

            layer_name_str = str(layer_name).strip()
            if not layer_name_str or layer_name_str in seen:
                continue

            seen.add(layer_name_str)
            out.append({
                "layerId": i,
                "name": layer_name_str,
                "title": str(title).strip() if title else layer_name_str,
                "url": None,
                "queryable": bool(lyr.get("queryable", True)),
                "showPopup": bool(lyr.get("showPopup", False)),
                "legendUrl": lyr.get("legendUrl"),
            })

        elif isinstance(lyr, str):
            layer_name_str = lyr.strip()
            if not layer_name_str or layer_name_str in seen:
                continue

            seen.add(layer_name_str)
            out.append({
                "layerId": i,
                "name": layer_name_str,
                "title": layer_name_str,
                "url": None,
                "queryable": True,
                "showPopup": False,
                "legendUrl": None,
            })

    if out:
        return out

    item_url = _get_item_url(item)
    if not item_url:
        return []

    caps_url = _build_wms_capabilities_url(item_url)

    try:
        if gis:
            xml_text = gis._con.get(caps_url, try_json=False)
        else:
            response = requests.get(caps_url, timeout=30)
            response.raise_for_status()
            xml_text = response.text

        if isinstance(xml_text, bytes):
            xml_text = xml_text.decode("utf-8", errors="replace")

        if isinstance(xml_text, str) and xml_text.strip():
            return _parse_wms_layers_from_capabilities_xml(xml_text)
    except Exception:
        pass

    return []


def _classify_by_url(url: Optional[str]) -> str:
    if not url:
        return "other"

    u = url.lower()

    if "/vectortileserver" in u:
        return "vectortile"
    if "/featureserver" in u:
        return "feature"
    if "wms" in u:
        return "wms"
    if "/mapserver" in u:
        return "mapimage"

    return "other"


def _classify_layer_obj(layer_obj: dict) -> str:
    layer_type = (layer_obj.get("layerType") or "").lower()
    style_url = layer_obj.get("styleUrl")
    url = layer_obj.get("url")

    if layer_type == "arcgistiledmapservicelayer":
        return "tile"
    if "vectortile" in layer_type:
        return "vectortile"
    if layer_type == "wms":
        return "wms"
    if "mapservice" in layer_type:
        return "mapimage"
    if "feature" in layer_type:
        return "feature"
    if isinstance(style_url, str) and "/vectortileserver" in style_url.lower():
        return "vectortile"

    return _classify_by_url(url)


def _classify_item(item, item_url: Optional[str], gis=None) -> str:
    item_type = (getattr(item, "type", "") or "").lower()

    if "vector tile" in item_type:
        return "vectortile"

    if "wms" in item_type:
        return "wms"

    if "tile layer" in item_type or "map tile layer" in item_type:
        return "tile"

    if item_url and "/mapserver" in item_url.lower() and gis:
        mode = _detect_mapserver_mode(gis, item_url)
        if mode == "tile":
            return "tile"

    if "map image" in item_type or "map service" in item_type:
        return "mapimage"

    if "feature" in item_type:
        if item_url and "/mapserver" in item_url.lower() and gis:
            mode = _detect_mapserver_mode(gis, item_url)
            if mode in {"tile", "mapimage"}:
                return mode
        return "feature"

    try:
        type_keywords = getattr(item, "typeKeywords", None) or []
        joined = " ".join(type_keywords).lower()

        if "vectortile" in joined or "vector tile" in joined:
            return "vectortile"

        if "tile layer" in joined or "tiles" in joined or "tiled" in joined or "cached" in joined:
            return "tile"

        if "mapimage" in joined or "map service" in joined:
            return "mapimage"

        if "feature service" in joined or "feature layer" in joined:
            if item_url and "/mapserver" in (item_url or "").lower() and gis:
                mode = _detect_mapserver_mode(gis, item_url)
                if mode in {"tile", "mapimage"}:
                    return mode
            return "feature"

        if "wms" in joined:
            return "wms"
    except Exception:
        pass

    kind = _classify_by_url(item_url)
    if kind == "mapimage" and gis and item_url and "/mapserver" in item_url.lower():
        mode = _detect_mapserver_mode(gis, item_url)
        if mode in {"tile", "mapimage"}:
            return mode

    if kind != "other":
        return kind

    return "other"


def _fetch_old_item_titles(gis, old_ids: Set[str], runtime_issues: List[str]) -> Dict[str, str]:
    titles = {}
    for old_id in sorted(old_ids):
        try:
            item = gis.content.get(old_id)
            if item and getattr(item, "title", None):
                titles[old_id] = str(item.title).strip()
            else:
                _add_runtime_issue(runtime_issues, f"Alter Layer-Titel konnte nicht geladen werden: {old_id}")
        except Exception as e:
            _add_runtime_issue(runtime_issues, f"Alter Layer-Titel konnte nicht geladen werden: {old_id} | {e}")
    return titles


def _detect_target_structure_mode(item_type: str, sublayers: List[dict]) -> str:
    if item_type == "feature":
        return "group_children" if len(sublayers) > 1 else "single"

    if item_type == "mapimage":
        return "service_with_layers" if len(sublayers) > 0 else "single"

    if item_type == "wms":
        return "wms_with_layers" if len(sublayers) > 0 else "single"

    if item_type == "tile":
        return "single"

    return "single"


def _validate_target_url_for_type(target_info: dict, runtime_issues: List[str]):
    url = target_info.get("url")
    if not isinstance(url, str) or not url.strip():
        return

    u = url.lower().rstrip("/")

    if target_info["type"] in {"mapimage", "tile"} and "/mapserver" not in u:
        _add_runtime_issue(runtime_issues, f"Ziel-URL wirkt ungewoehnlich fuer {target_info['type']}: {url}")

    if target_info["type"] == "feature" and "/featureserver" not in u:
        _add_runtime_issue(runtime_issues, f"Ziel-URL wirkt ungewoehnlich fuer feature: {url}")

    if target_info["type"] == "vectortile" and "/vectortileserver" not in u:
        _add_runtime_issue(runtime_issues, f"Ziel-URL wirkt ungewoehnlich fuer vectortile: {url}")


def _analyze_new_target(item, gis, runtime_issues: List[str]):
    item_url = _get_item_url(item)
    item_type = _classify_item(item, item_url, gis)
    item_title = getattr(item, "title", None)

    sublayers = []
    wms_meta = {}

    if item_type == "feature":
        sublayers = _get_feature_sublayers(item)
    elif item_type == "mapimage":
        sublayers = _get_service_sublayers(gis, item_url)
    elif item_type == "wms":
        sublayers = _get_wms_sublayers_from_item(item, gis)
        wms_meta = _get_wms_item_metadata(item)

    structure_mode = _detect_target_structure_mode(item_type, sublayers)

    target_info = {
        "item": item,
        "itemId": item.id,
        "title": item_title,
        "url": wms_meta.get("url") if item_type == "wms" else item_url,
        "mapUrl": wms_meta.get("mapUrl") if item_type == "wms" else None,
        "featureInfoUrl": wms_meta.get("featureInfoUrl") if item_type == "wms" else None,
        "featureInfoFormat": wms_meta.get("featureInfoFormat") if item_type == "wms" else None,
        "spatialReferences": wms_meta.get("spatialReferences") if item_type == "wms" else None,
        "version": wms_meta.get("version") if item_type == "wms" else None,
        "format": wms_meta.get("format") if item_type == "wms" else None,
        "styleUrl": _get_vector_tile_style_url(item),
        "type": item_type,
        "sublayers": sublayers,
        "has_sublayers": len(sublayers) > 0,
        "sublayer_count": len(sublayers),
        "structure_mode": structure_mode,
    }

    _validate_target_url_for_type(target_info, runtime_issues)
    return target_info


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


# =========================================================
# Treffererkennung
# =========================================================
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
    result = {
        "is_candidate": False,
        "matched_old_id": None,
        "has_foreign_ids": False,
        "foreign_ids": set(),
    }

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


# =========================================================
# Unterlayer-Matching
# =========================================================
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


def _build_expected_new_sublayer_signatures(target_info: dict) -> List[Tuple[str, Optional[int]]]:
    sigs = []
    for sub in target_info.get("sublayers", []):
        sigs.append((_normalize_text(sub.get("title")), sub.get("layerId")))
    return sigs


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
            removed += _remove_matching_sublayers_outside_path(
                children,
                expected_signatures,
                skip_exact_path,
                current_path + ".layers",
            )

        kept.append(lyr)

    layers[:] = kept
    return removed


# =========================================================
# Tile-Sonderweg - reduziert
# =========================================================
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

    allowed = (set(SAFE_KEYS) - {"id"}) | TARGET_TYPE_KEYS.get("tile", set())
    allowed -= NEVER_COPY_KEYS

    for k in allowed:
        if k in old_layer_obj:
            fresh[k] = _json_clone(old_layer_obj[k])

    for bad_key in (
        "featureCollection",
        "layers",
        "visibleLayers",
        "layerDefinition",
        "popupInfo",
        "serviceItemId",
        "disablePopup",
        "visibilityMode",
    ):
        fresh.pop(bad_key, None)

    return fresh


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
            removed += _dedupe_target_tile_layers(
                children,
                target_item_id=target_item_id,
                keep_path=keep_path,
                path=current_path + ".layers",
            )

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


# =========================================================
# Block-Builder
# =========================================================
def _build_structured_feature_child_layer(old_child: Optional[dict], sub: dict, target_info: dict) -> dict:
    target_item_id = target_info["itemId"]

    if old_child:
        old_child_type = _classify_layer_obj(old_child)
        transfer_props = _collect_transferable_props(old_child, old_child_type, "feature")
    else:
        transfer_props = {}

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
    children = []
    old_by_title = _index_old_group_children_by_title(old_layer_obj)

    for sub in target_info["sublayers"]:
        old_child = _find_matching_old_child_for_new_sub(sub, old_by_title)
        if old_child:
            child = _build_structured_feature_child_layer(old_child, sub, target_info)
        else:
            child = _build_structured_feature_child_layer(None, sub, target_info)
        children.append(child)

    children.reverse()
    return children


def _build_feature_group_block(old_layer_obj: dict, target_info: dict, old_type: str) -> dict:
    fresh = {}
    parent_safe = _copy_group_parent_safe_props(old_layer_obj, old_type)
    fresh.update(parent_safe)

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
    new_title = target_info["title"]
    new_url = _build_new_url(old_layer_obj, target_info)

    if not new_url and new_type not in {"vectortile"}:
        raise RuntimeError("Neue URL fehlt.")

    fresh = {}
    transfer_props = _collect_transferable_props(old_layer_obj, old_type, new_type)
    fresh.update(transfer_props)

    fresh["title"] = new_title if (UPDATE_TITLE_TO_NEW and new_title) else old_layer_obj.get("title")

    for k in NEVER_COPY_KEYS:
        fresh.pop(k, None)

    if new_type == "feature":
        fresh["layerType"] = "ArcGISFeatureLayer"
        fresh["itemId"] = new_id
        fresh["serviceItemId"] = new_id
        if new_url:
            fresh["url"] = new_url

    elif new_type == "mapimage":
        fresh = _build_clean_base_props_for_service_target(
            old_layer_obj=old_layer_obj,
            new_title=new_title if (UPDATE_TITLE_TO_NEW and new_title) else old_layer_obj.get("title"),
            fallback_id=target_info["itemId"],
        )
        fresh["layerType"] = "ArcGISMapServiceLayer"
        fresh["itemId"] = new_id
        if new_url:
            fresh["url"] = new_url

    elif new_type == "tile":
        raise RuntimeError("Tile wird im Sonderweg behandelt und darf hier nicht landen.")

    elif new_type == "wms":
        raise RuntimeError("WMS mit Unterlayern wird ueber Sonderweg gebaut und darf hier nicht landen.")

    elif new_type == "vectortile":
        fresh["layerType"] = "VectorTileLayer"
        fresh["itemId"] = new_id
        fresh["serviceItemId"] = new_id
        if new_url:
            fresh["url"] = new_url
        if target_info.get("styleUrl"):
            fresh["styleUrl"] = target_info["styleUrl"]

    else:
        raise RuntimeError(f"Zieltyp '{new_type}' wird nicht unterstuetzt.")

    return fresh


def _build_mapimage_service_with_layers_block(old_layer_obj: dict, target_info: dict, old_type: str) -> dict:
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
    fresh = {}

    transfer_props = _collect_transferable_props(old_layer_obj, old_type, "wms")
    fresh.update(transfer_props)

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

    if visible_names:
        fresh["visibleLayers"] = visible_names
    else:
        fresh["visibleLayers"] = [lyr["name"] for lyr in wms_layers]

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
    transferred_keys = sorted([
        k for k in fresh.keys()
        if k in before and k not in {"title", "url", "itemId", "serviceItemId", "layerType", "layers", "visibilityMode"}
    ])

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


# =========================================================
# Walker
# =========================================================
def _walk_and_replace(
    layers,
    old_ids,
    old_item_titles,
    target_info,
    context,
    path="operationalLayers",
):
    hits = []

    if not isinstance(layers, list):
        return hits

    for i, lyr in enumerate(layers):
        if not isinstance(lyr, dict):
            continue

        p = f"{path}[{i}]"

        matched_old_id = _get_direct_matched_old_id(lyr, old_ids, old_item_titles)
        if matched_old_id:
            old_type = _classify_layer_obj(lyr)

            if target_info["type"] == "tile":
                before = _json_clone(lyr)
                fresh = _build_minimal_tile_block(before, target_info)

                lyr.clear()
                lyr.update(fresh)

                context["tile_keep_path"] = p

                hits.append({
                    "path": p,
                    "matched_old_id": matched_old_id,
                    "old_item_title_filter": old_item_titles.get(matched_old_id),
                    "old_type": old_type,
                    "new_type": target_info["type"],
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
                })
                continue

            use_soft = (
                old_type == "feature"
                and target_info["type"] == "feature"
                and target_info["structure_mode"] == "single"
            )

            if use_soft:
                result = _soft_replace_feature(lyr, matched_old_id, target_info)
            else:
                result = _hard_replace_with_built_block(lyr, target_info, old_type)

            if result.get("group_replaced"):
                context["group_layer_replaced"] = True

            hits.append({
                "path": p,
                "matched_old_id": matched_old_id,
                "old_item_title_filter": old_item_titles.get(matched_old_id),
                "old_type": old_type,
                "new_type": target_info["type"],
                **result,
            })
            continue

        group_info = _analyze_group_replace_candidate(lyr, old_ids, old_item_titles)
        if group_info["is_candidate"]:
            if group_info["has_foreign_ids"]:
                context["has_group_conflict"] = True
                continue

            if target_info["type"] == "tile":
                before = _json_clone(lyr)
                fresh = _build_minimal_tile_block(before, target_info)

                lyr.clear()
                lyr.update(fresh)

                context["tile_keep_path"] = p

                hits.append({
                    "path": p,
                    "matched_old_id": group_info["matched_old_id"],
                    "old_item_title_filter": old_item_titles.get(group_info["matched_old_id"]),
                    "old_type": "group-parent",
                    "new_type": target_info["type"],
                    "mode": "TILE_MINIMAL_REPLACE(group-parent->tile)",
                    "changed": True,
                    "before_title": before.get("title"),
                    "after_title": lyr.get("title"),
                    "before_url": before.get("url"),
                    "after_url": lyr.get("url"),
                    "transferred_keys": None,
                    "dropped_keys": sorted([k for k in before.keys() if k not in lyr]),
                    "group_replaced": False,
                    "final_block": _json_clone(lyr),
                })
                continue

            removed_before_replace = 0
            if target_info["structure_mode"] == "group_children":
                expected_signatures = _build_expected_new_sublayer_signatures(target_info)
                removed_before_replace = _remove_matching_sublayers_outside_path(
                    context["root_operational_layers"],
                    expected_signatures,
                    p,
                    path="operationalLayers",
                )

            result = _hard_replace_with_built_block(lyr, target_info, "group-parent")
            context["group_layer_replaced"] = True

            hits.append({
                "path": p,
                "matched_old_id": group_info["matched_old_id"],
                "old_item_title_filter": old_item_titles.get(group_info["matched_old_id"]),
                "old_type": "group-parent",
                "new_type": target_info["type"],
                "removed_before_replace": removed_before_replace,
                **result,
            })
            continue

        if lyr.get("layerType") == "GroupLayer" and isinstance(lyr.get("layers"), list):
            hits.extend(
                _walk_and_replace(
                    lyr["layers"],
                    old_ids,
                    old_item_titles,
                    target_info,
                    context,
                    p + ".layers",
                )
            )

    return hits


# =========================================================
# Fachfunktion
# =========================================================
def run_layer_replacement(
    old_layer_itemids: Set[str],
    new_layer_itemid: str,
    dry_run: bool = True,
    message_func=None,
    warning_func=None,
):
    runtime_issues: List[str] = []

    portal_url = arcpy.GetActivePortalURL()
    if not portal_url:
        raise RuntimeError("Es konnte kein aktives Portal aus ArcGIS Pro ermittelt werden.")

    token_info = arcpy.GetSigninToken()
    if token_info is None:
        raise RuntimeError(
            "In ArcGIS Pro ist kein Portal-Login aktiv. "
            "Bitte zuerst in ArcGIS Pro am Portal anmelden."
        )

    try:
        gis = GIS(
            url=portal_url,
            token=token_info["token"],
            referer=token_info.get("referer")
        )
    except Exception as e:
        raise RuntimeError(f"Anmeldung am aktiven Portal fehlgeschlagen: {e}")

    _msg(message_func, f"Python-Version: {sys.version}")
    _msg(message_func, f"arcgis-Version: {arcgis.__version__}")
    _msg(message_func, f"requests-Version: {requests.__version__}")

    owner = gis.users.me.username
    _msg(message_func, f"Eingeloggt als: {owner}")
    _msg(message_func, f"Portal: {portal_url}")
    _msg(message_func, "Es werden nur WebMaps dieses Owners verarbeitet.")

    if not old_layer_itemids:
        raise RuntimeError("Es wurde keine alte Layer-ID uebergeben.")

    old_item_titles = _fetch_old_item_titles(gis, old_layer_itemids, runtime_issues)

    new_item = gis.content.get(new_layer_itemid)
    if not new_item:
        raise RuntimeError("Neue Layer-ID nicht gefunden.")

    target_info = _analyze_new_target(new_item, gis, runtime_issues)

    if target_info["type"] == "wms" and not target_info["sublayers"]:
        raise RuntimeError(
            "Das WMS-Ziel liefert keine auswertbaren Sublayer. "
            "Automatischer Austausch wird aus Sicherheitsgruenden abgebrochen."
        )

    _msg(message_func, "")
    _msg(message_func, f"Alte Layer-IDs: {', '.join(sorted(old_layer_itemids))}")

    if old_item_titles:
        _msg(message_func, "Automatische Titel-Filter aus alten IDs:")
        for old_id in sorted(old_item_titles):
            _msg(message_func, f"  {old_id} -> {old_item_titles[old_id]}")
    else:
        _msg(message_func, "Automatische Titel-Filter: keine verfuegbar")
        _add_runtime_issue(
            runtime_issues,
            "Fuer keine alte Layer-ID konnte ein Titel geladen werden. Titel-Matching wird dadurch unvollstaendig."
        )

    _msg(message_func, f"Neues Item: {new_item.title} ({new_item.id})")
    _msg(message_func, f"Neuer Item-Type: {getattr(new_item, 'type', None)}")
    _msg(message_func, f"Neuer Typ: {target_info['type']}")
    _msg(message_func, f"Neue URL: {target_info['url']}")

    if target_info["type"] == "vectortile":
        _msg(message_func, f"Neue styleUrl: {target_info.get('styleUrl')}")

    if target_info["type"] == "wms":
        _msg(message_func, f"WMS mapUrl: {target_info.get('mapUrl')}")
        _msg(message_func, f"WMS version: {target_info.get('version')}")
        _msg(message_func, f"WMS Unterlayernamen: {', '.join([s.get('name', '') for s in target_info['sublayers'] if s.get('name')])}")

    _msg(message_func, f"Ziel hat Unterlayer: {target_info['has_sublayers']}")
    _msg(message_func, f"Anzahl Unterlayer: {target_info['sublayer_count']}")
    _msg(message_func, f"Strukturmodus: {target_info['structure_mode']}")

    try:
        _msg(message_func, f"Neue typeKeywords: {getattr(new_item, 'typeKeywords', None)}")
    except Exception:
        pass

    if (getattr(new_item, "type", "") or "").lower() == "web map":
        raise RuntimeError(
            "Die neue Layer-ID zeigt auf eine WebMap, nicht auf ein Layer-Item. "
            "Bitte die Item-ID des eigentlichen Ziel-Layers eintragen."
        )

    if target_info["type"] == "other":
        raise RuntimeError(
            f"Die neue Layer-ID konnte keinem unterstuetzten Typ zugeordnet werden. "
            f"Item-Type: {getattr(new_item, 'type', None)}"
        )

    if not target_info["url"] and target_info["type"] != "vectortile":
        raise RuntimeError(
            f"Fuer die neue Layer-ID konnte keine URL ermittelt werden. "
            f"Item-Type: {getattr(new_item, 'type', None)}"
        )

    try:
        webmaps = gis.content.search(
            query=f'type:"Web Map" AND owner:{owner}',
            max_items=5000
        )
    except Exception as e:
        raise RuntimeError(f"WebMaps konnten nicht gesucht werden: {e}")

    touched = 0
    updated = 0
    conflict_webmaps = []

    for wm in webmaps:
        try:
            _msg(message_func, f"DEBUG vor get_data: {wm.title} ({wm.id})")
            try:
                data = wm.get_data()
                _msg(message_func, f"DEBUG nach get_data: {wm.title} ({wm.id})")
            except Exception as e:
                _warn(warning_func, f"FEHLER in get_data bei {wm.title} ({wm.id}): {e}")
                _add_runtime_issue(runtime_issues, f"get_data fehlgeschlagen: {wm.title} ({wm.id}) | {e}")
                continue

            if not isinstance(data, dict):
                _warn(warning_func, f"Uebersprungen (ungueltige Daten): {wm.title} ({wm.id})")
                _add_runtime_issue(runtime_issues, f"WebMap hat keine gueltigen JSON-Daten: {wm.title} ({wm.id})")
                continue

            try:
                original_data = _json_clone(data)
                _msg(message_func, f"DEBUG nach json_clone: {wm.title} ({wm.id})")
            except Exception as e:
                _warn(warning_func, f"FEHLER in json_clone bei {wm.title} ({wm.id}): {e}")
                _add_runtime_issue(runtime_issues, f"json_clone fehlgeschlagen: {wm.title} ({wm.id}) | {e}")
                continue

            context = {
                "has_group_conflict": False,
                "group_layer_replaced": False,
                "root_operational_layers": data.get("operationalLayers", []),
                "tile_keep_path": None,
            }

            hits = _walk_and_replace(
                data.get("operationalLayers", []),
                old_layer_itemids,
                old_item_titles,
                target_info,
                context,
            )

            cleanup_removed = 0

            if context["has_group_conflict"]:
                conflict_webmaps.append((wm.title, wm.id))
                _warn(
                    warning_func,
                    f"GroupLayer-Konflikt erkannt -> WebMap bleibt unveraendert: {wm.title} ({wm.id})"
                )
                data = original_data
                continue

            if target_info["type"] == "tile" and context.get("tile_keep_path"):
                cleanup_removed = _dedupe_target_tile_layers(
                    data.get("operationalLayers", []),
                    target_item_id=target_info["itemId"],
                    keep_path=context["tile_keep_path"],
                    path="operationalLayers",
                )
                if cleanup_removed > 0:
                    _msg(message_func, f"DEBUG entfernte konkurrierende Ziel-Layer fuer Tile: {cleanup_removed}")

            if not hits and cleanup_removed == 0:
                continue

            touched += 1
            _msg(message_func, "")
            _msg(message_func, f"WebMap: {wm.title} ({wm.id}) Treffer: {len(hits)}")

            for h in hits:
                _msg(message_func, f"- {h['path']} | alte ID: {h['matched_old_id']} | {h['mode']} | {h['old_type']}->{h['new_type']}")
                if h.get("old_item_title_filter"):
                    _msg(message_func, f"  Titel-Filter: {h['old_item_title_filter']}")
                _msg(message_func, f"  Titel: {h['before_title']} -> {h['after_title']}")
                _msg(message_func, f"  URL:   {h['before_url']} -> {h['after_url']}")

                if h.get("removed_before_replace", 0) > 0:
                    _msg(message_func, f"  vor dem Ersetzen entfernte ausgelagerte Unterlayer: {h['removed_before_replace']}")

                if h["transferred_keys"] is not None:
                    _msg(message_func, f"  uebernommen: {', '.join(h['transferred_keys']) if h['transferred_keys'] else '-'}")
                if h["dropped_keys"] is not None:
                    _msg(message_func, f"  verworfen: {', '.join(h['dropped_keys']) if h['dropped_keys'] else '-'}")

                if DEBUG_DUMP_FINAL_BLOCKS and h.get("final_block") is not None:
                    _msg(message_func, "  Finaler Layerblock:")
                    _msg(message_func, json.dumps(h["final_block"], ensure_ascii=False, indent=2))

            if target_info["type"] == "tile":
                final_ok, final_messages = _validate_final_tile_targets(data, target_info)
                for msg in final_messages:
                    _warn(warning_func, f"{wm.title} ({wm.id}) | {msg}")
                    _add_runtime_issue(runtime_issues, f"{wm.title} ({wm.id}) | {msg}")

                if not final_ok:
                    _warn(
                        warning_func,
                        f"Tile-Endvalidierung fehlgeschlagen -> WebMap bleibt unveraendert: {wm.title} ({wm.id})"
                    )
                    data = original_data
                    continue

            if dry_run:
                _msg(message_func, "DRY_RUN=True -> keine Speicherung.")
                continue

            _msg(message_func, f"DEBUG vor wm.update: {wm.title} ({wm.id})")
            try:
                ok = wm.update(data=data)
                _msg(message_func, f"DEBUG nach wm.update: {wm.title} ({wm.id}) | ok={ok}")
            except Exception as e:
                _warn(warning_func, f"FEHLER in wm.update bei {wm.title} ({wm.id}): {e}")
                _add_runtime_issue(runtime_issues, f"wm.update fehlgeschlagen: {wm.title} ({wm.id}) | {e}")

                if USE_REST_UPDATE_FALLBACK:
                    _msg(message_func, f"DEBUG REST-Fallback gestartet: {wm.title} ({wm.id})")
                    try:
                        ok = _update_webmap_via_rest(gis, wm, data)
                        _msg(message_func, f"DEBUG REST-Fallback Ergebnis: {wm.title} ({wm.id}) | ok={ok}")
                    except Exception as rest_e:
                        _warn(warning_func, f"FEHLER im REST-Fallback bei {wm.title} ({wm.id}): {rest_e}")
                        _add_runtime_issue(runtime_issues, f"REST-Fallback fehlgeschlagen: {wm.title} ({wm.id}) | {rest_e}")
                        continue
                else:
                    continue

            _msg(message_func, f"Gespeichert: {ok}")
            if ok:
                updated += 1
            else:
                _add_runtime_issue(runtime_issues, f"WebMap konnte nicht gespeichert werden: {wm.title} ({wm.id})")

        except Exception as e:
            _warn(warning_func, f"Fehler in WebMap {wm.title} ({wm.id}): {e}")
            _add_runtime_issue(runtime_issues, f"Fehler bei der Verarbeitung einer WebMap: {wm.title} ({wm.id}) | {e}")

    return {
        "touched": touched,
        "updated": updated,
        "conflicts": conflict_webmaps,
        "runtime_issues": runtime_issues,
    }