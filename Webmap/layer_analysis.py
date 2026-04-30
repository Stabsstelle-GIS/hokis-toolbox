import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from typing import List, Optional

import requests
from arcgis.features import FeatureLayerCollection


def _normalize_text(value) -> str:
    return str(value).strip().casefold() if value is not None else ""


def _title_matches_exact(a, b) -> bool:
    return _normalize_text(a) == _normalize_text(b)


def _strip_query(url: Optional[str]) -> Optional[str]:
    if not isinstance(url, str):
        return url
    return url.split("?", 1)[0]


def _safe_get_item_data(item) -> dict:
    try:
        data = item.get_data()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_get_item_properties(item):
    try:
        return getattr(item, "properties", None)
    except Exception:
        return None


def _first_nonempty_str(*values):
    for val in values:
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _first_nonempty_list(*values):
    for val in values:
        if isinstance(val, list) and val:
            return val
    return None


def _safe_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _strip_xml_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _build_wms_capabilities_url(url: str) -> str:
    base = url.strip()
    sep = "&" if "?" in base else "?"
    return base + sep + urlencode({"service": "WMS", "request": "GetCapabilities"})


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


def _get_item_url(item):
    data = _safe_get_item_data(item)
    props = _safe_get_item_properties(item)

    url = _first_nonempty_str(
        getattr(item, "url", None),
        getattr(props, "url", None) if props else None,
        data.get("url"),
        data.get("serviceUrl"),
    )
    if url:
        return url

    try:
        flc = FeatureLayerCollection.fromitem(item)
        return _first_nonempty_str(getattr(flc, "url", None))
    except Exception:
        return None


def _get_vector_tile_style_url(item):
    data = _safe_get_item_data(item)
    style_url = _first_nonempty_str(data.get("styleUrl"))
    if style_url:
        return style_url

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
        clean_sub_ids = [_safe_int(sid) for sid in sub_ids if _safe_int(sid) is not None] if isinstance(sub_ids, list) else None
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
    data = _safe_get_item_data(item)
    props = _safe_get_item_properties(item)

    meta = {
        "url": _first_nonempty_str(data.get("url"), getattr(props, "url", None) if props else None, _get_item_url(item)),
        "mapUrl": _first_nonempty_str(data.get("mapUrl"), getattr(props, "mapUrl", None) if props else None),
        "featureInfoUrl": _first_nonempty_str(data.get("featureInfoUrl"), getattr(props, "featureInfoUrl", None) if props else None),
        "featureInfoFormat": _first_nonempty_str(data.get("featureInfoFormat"), getattr(props, "featureInfoFormat", None) if props else None),
        "spatialReferences": _first_nonempty_list(data.get("spatialReferences"), getattr(props, "spatialReferences", None) if props else None),
        "version": _first_nonempty_str(data.get("version"), getattr(props, "version", None) if props else None),
        "format": _first_nonempty_str(data.get("format"), getattr(props, "format", None) if props else None),
    }

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
    data = _safe_get_item_data(item)
    props = _safe_get_item_properties(item)
    candidates = []
    for val in (
        data.get("layers"),
        data.get("visibleLayers"),
        getattr(props, "layers", None) if props else None,
        getattr(props, "visibleLayers", None) if props else None,
    ):
        if isinstance(val, list):
            candidates.extend(val)

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


def _fetch_old_item_titles(gis, old_ids: set[str], runtime_issues: list[str]) -> dict[str, str]:
    titles = {}
    for old_id in sorted(old_ids):
        try:
            item = gis.content.get(old_id)
            if item and getattr(item, "title", None):
                titles[old_id] = str(item.title).strip()
            else:
                if f"Alter Layer-Titel konnte nicht geladen werden: {old_id}" not in runtime_issues:
                    runtime_issues.append(f"Alter Layer-Titel konnte nicht geladen werden: {old_id}")
        except Exception as e:
            msg = f"Alter Layer-Titel konnte nicht geladen werden: {old_id} | {e}"
            if msg not in runtime_issues:
                runtime_issues.append(msg)
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


def _validate_target_url_for_type(target_info: dict, runtime_issues: list[str]):
    url = target_info.get("url")
    if not isinstance(url, str) or not url.strip():
        return

    u = url.lower().rstrip("/")
    if target_info["type"] in {"mapimage", "tile"} and "/mapserver" not in u:
        msg = f"Ziel-URL wirkt ungewoehnlich fuer {target_info['type']}: {url}"
        if msg not in runtime_issues:
            runtime_issues.append(msg)
    if target_info["type"] == "feature" and "/featureserver" not in u:
        msg = f"Ziel-URL wirkt ungewoehnlich fuer feature: {url}"
        if msg not in runtime_issues:
            runtime_issues.append(msg)
    if target_info["type"] == "vectortile" and "/vectortileserver" not in u:
        msg = f"Ziel-URL wirkt ungewoehnlich fuer vectortile: {url}"
        if msg not in runtime_issues:
            runtime_issues.append(msg)


def _analyze_new_target(item, gis, runtime_issues: list[str]):
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