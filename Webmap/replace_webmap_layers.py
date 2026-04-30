from typing import List, Set

import arcpy
from arcgis.gis import GIS

from layer_analysis import _fetch_old_item_titles, _analyze_new_target
from layer_matching import _walk_and_replace
from layer_replace_builders import (
    _msg,
    _warn,
    _add_runtime_issue,
    _json_clone,
    _update_webmap_via_rest,
    _dedupe_target_tile_layers,
    _validate_final_tile_targets,
)

# Feste Konfiguration
USE_REST_UPDATE_FALLBACK = True


def run_layer_replacement(old_layer_itemid: str, new_layer_itemid: str, dry_run: bool = True, message_func=None, warning_func=None):
    runtime_issues: List[str] = []

    portal_url = arcpy.GetActivePortalURL()
    if not portal_url:
        raise RuntimeError("Es konnte kein aktives Portal aus ArcGIS Pro ermittelt werden.")

    token_info = arcpy.GetSigninToken()
    if token_info is None:
        raise RuntimeError("In ArcGIS Pro ist kein Portal-Login aktiv. Bitte zuerst in ArcGIS Pro am Portal anmelden.")

    try:
        gis = GIS(url=portal_url, token=token_info["token"], referer=token_info.get("referer"))
    except Exception as e:
        raise RuntimeError(f"Anmeldung am aktiven Portal fehlgeschlagen: {e}")

    owner = gis.users.me.username

    old_layer_itemid = old_layer_itemid.strip() if old_layer_itemid else ""
    if not old_layer_itemid:
        raise RuntimeError("Es wurde keine alte Layer-ID uebergeben.")

    old_ids: Set[str] = {old_layer_itemid}
    new_layer_itemid = new_layer_itemid.strip() if new_layer_itemid else ""
    if not new_layer_itemid:
        raise RuntimeError("Es wurde keine neue Layer-ID uebergeben.")

    old_item_titles = _fetch_old_item_titles(gis, old_ids, runtime_issues)
    new_item = gis.content.get(new_layer_itemid)
    if not new_item:
        raise RuntimeError("Neue Layer-ID nicht gefunden.")

    target_info = _analyze_new_target(new_item, gis, runtime_issues)

    if target_info["type"] == "wms" and not target_info["sublayers"]:
        raise RuntimeError("Das WMS-Ziel liefert keine auswertbaren Sublayer. Automatischer Austausch wird aus Sicherheitsgruenden abgebrochen.")

    if (getattr(new_item, "type", "") or "").lower() == "web map":
        raise RuntimeError("Die neue Layer-ID zeigt auf eine WebMap, nicht auf ein Layer-Item. Bitte die Item-ID des eigentlichen Ziel-Layers eintragen.")

    if target_info["type"] == "other":
        raise RuntimeError(f"Die neue Layer-ID konnte keinem unterstuetzten Typ zugeordnet werden. Item-Type: {getattr(new_item, 'type', None)}")

    if not target_info["url"] and target_info["type"] != "vectortile":
        raise RuntimeError(f"Fuer die neue Layer-ID konnte keine URL ermittelt werden. Item-Type: {getattr(new_item, 'type', None)}")

    try:
        webmaps = gis.content.search(query=f'type:"Web Map" AND owner:{owner}', max_items=5000)
    except Exception as e:
        raise RuntimeError(f"WebMaps konnten nicht gesucht werden: {e}")

    touched = 0
    updated = 0
    conflict_webmaps = []
    touched_webmaps = []
    old_layer_type = "unbekannt"

    for wm in webmaps:
        try:
            try:
                data = wm.get_data()
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
            hits = _walk_and_replace(data.get("operationalLayers", []), old_ids, old_item_titles, target_info, context)
            cleanup_removed = 0

            if hits and old_layer_type == "unbekannt":
                old_layer_type = hits[0].get("old_type") or "unbekannt"

            if context["has_group_conflict"]:
                conflict_webmaps.append((wm.title, wm.id))
                data = original_data
                continue

            if target_info["type"] == "tile" and context.get("tile_keep_path"):
                cleanup_removed = _dedupe_target_tile_layers(
                    data.get("operationalLayers", []),
                    target_item_id=target_info["itemId"],
                    keep_path=context["tile_keep_path"],
                    path="operationalLayers",
                )

            if not hits and cleanup_removed == 0:
                continue

            touched += 1
            touched_webmaps.append((wm.title, wm.id))

            if target_info["type"] == "tile":
                final_ok, final_messages = _validate_final_tile_targets(data, target_info)
                for msg in final_messages:
                    _warn(warning_func, f"{wm.title} ({wm.id}) | {msg}")
                    _add_runtime_issue(runtime_issues, f"{wm.title} ({wm.id}) | {msg}")

                if not final_ok:
                    data = original_data
                    touched_webmaps.pop()
                    touched -= 1
                    conflict_webmaps.append((wm.title, wm.id))
                    continue

            if dry_run:
                continue

            try:
                ok = wm.update(data=data)
            except Exception as e:
                _warn(warning_func, f"FEHLER in wm.update bei {wm.title} ({wm.id}): {e}")
                _add_runtime_issue(runtime_issues, f"wm.update fehlgeschlagen: {wm.title} ({wm.id}) | {e}")

                if USE_REST_UPDATE_FALLBACK:
                    try:
                        ok = _update_webmap_via_rest(gis, wm, data)
                    except Exception as rest_e:
                        _warn(warning_func, f"FEHLER im REST-Fallback bei {wm.title} ({wm.id}): {rest_e}")
                        _add_runtime_issue(runtime_issues, f"REST-Fallback fehlgeschlagen: {wm.title} ({wm.id}) | {rest_e}")
                        continue
                else:
                    continue

            if ok:
                updated += 1
            else:
                _add_runtime_issue(runtime_issues, f"WebMap konnte nicht gespeichert werden: {wm.title} ({wm.id})")

        except Exception as e:
            _warn(warning_func, f"Fehler in WebMap {wm.title} ({wm.id}): {e}")
            _add_runtime_issue(runtime_issues, f"Fehler bei der Verarbeitung einer WebMap: {wm.title} ({wm.id}) | {e}")

    old_layer_name = old_item_titles.get(old_layer_itemid) or "-"

    _msg(message_func, "")
    _msg(message_func, "===== Layer-Austausch =====")
    _msg(message_func, "")
    _msg(message_func, "Alter Layer:")
    _msg(message_func, f"  ID: {old_layer_itemid}")
    _msg(message_func, f"  Name: {old_layer_name}")
    _msg(message_func, f"  Typ: {old_layer_type}")
    _msg(message_func, "")
    _msg(message_func, "Neuer Layer:")
    _msg(message_func, f"  ID: {new_item.id}")
    _msg(message_func, f"  Name: {getattr(new_item, 'title', '-') or '-'}")
    _msg(message_func, f"  Typ: {target_info['type']}")
    _msg(message_func, "")
    _msg(message_func, f"Dry Run: {'Ja' if dry_run else 'Nein'}")
    _msg(message_func, f"Maps mit Treffern: {touched} | Aktualisierte Maps: {updated} | Konflikt-WebMaps: {len(conflict_webmaps)}")

    if touched_webmaps:
        _msg(message_func, "")
        _msg(message_func, "Bearbeitete WebMaps:")
        for title, wm_id in touched_webmaps:
            _msg(message_func, f"  Name: {title}")
            _msg(message_func, f"  ID: {wm_id}")
            _msg(message_func, "")

    if conflict_webmaps:
        _msg(message_func, "Konflikt-WebMaps:")
        for title, wm_id in conflict_webmaps:
            _msg(message_func, f"  Name: {title}")
            _msg(message_func, f"  ID: {wm_id}")
            _msg(message_func, "")

    return {
        "touched": touched,
        "updated": updated,
        "conflicts": conflict_webmaps,
        "runtime_issues": runtime_issues,
    }
