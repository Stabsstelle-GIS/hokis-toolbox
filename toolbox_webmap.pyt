import arcpy


class Toolbox(object):
    def __init__(self):
        self.label = "WebMap Layer Replace Toolbox"
        self.alias = "webmapreplace"
        self.tools = [ReplaceWebMapLayersTool]


class ReplaceWebMapLayersTool(object):
    def __init__(self):
        self.label = "Veraltete Layer-Referenzen in WebMaps ersetzen"
        self.description = (
            "Ersetzt in WebMaps veraltete Layer-Referenzen kontrolliert "
            "durch einen neuen Ziel-Layer."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        param_old_ids = arcpy.Parameter(
            displayName="Alte Layer-IDs",
            name="old_layer_ids",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
            multiValue=True,
        )

        param_new_id = arcpy.Parameter(
            displayName="Neue Layer-ID",
            name="new_layer_id",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )

        param_dry_run = arcpy.Parameter(
            displayName="Dry Run",
            name="dry_run",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        param_dry_run.value = False

        param_summary = arcpy.Parameter(
            displayName="Zusammenfassung",
            name="summary",
            datatype="GPString",
            parameterType="Derived",
            direction="Output",
        )

        return [param_old_ids, param_new_id, param_dry_run, param_summary]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        if not parameters[2].altered:
            parameters[2].value = False
        return

    def updateMessages(self, parameters):
        old_ids = parameters[0].values if parameters[0].values else []
        new_id = parameters[1].valueAsText

        if not old_ids:
            parameters[0].setErrorMessage("Mindestens eine alte Layer-ID ist erforderlich.")
        else:
            invalid_old = []
            for old_id in old_ids:
                if old_id and len(old_id.strip()) != 32:
                    invalid_old.append(old_id)
            if invalid_old:
                parameters[0].setWarningMessage(
                    "Mindestens eine alte Layer-ID hat nicht die erwartete Laenge von 32 Zeichen."
                )

        if new_id:
            if len(new_id.strip()) != 32:
                parameters[1].setWarningMessage(
                    "Die neue Layer-ID hat nicht die erwartete Laenge von 32 Zeichen."
                )

            if old_ids and new_id in old_ids:
                parameters[1].setWarningMessage(
                    "Die neue Layer-ID ist auch in der Liste der alten Layer-IDs enthalten."
                )

        return

    def execute(self, parameters, messages):
        try:
            from replace_webmap_layers import run_layer_replacement
        except Exception as e:
            arcpy.AddError(f"Import von replace_webmap_layers.py fehlgeschlagen: {e}")
            raise

        old_layer_ids = set([x.strip() for x in (parameters[0].values or []) if x and x.strip()])
        new_layer_id = parameters[1].valueAsText.strip()

        dry_run = bool(parameters[2].value) if parameters[2].value is not None else False

        try:
            result = run_layer_replacement(
                old_layer_itemids=old_layer_ids,
                new_layer_itemid=new_layer_id,
                dry_run=dry_run,
                message_func=arcpy.AddMessage,
                warning_func=arcpy.AddWarning,
            )

            summary = (
                f"Maps mit Treffern: {result['touched']} | "
                f"Aktualisierte Maps: {result['updated']} | "
                f"Konflikt-WebMaps: {len(result['conflicts'])} | "
                f"Laufzeit-Hinweise: {len(result['runtime_issues'])}"
            )

            arcpy.AddMessage("")
            arcpy.AddMessage("===== Abschluss =====")
            arcpy.AddMessage(summary)

            if result["conflicts"]:
                arcpy.AddWarning("")
                arcpy.AddWarning("WebMaps mit GroupLayer-Konflikt (unveraendert gelassen):")
                for title, wm_id in result["conflicts"]:
                    arcpy.AddWarning(f"- {title} ({wm_id})")

            if result["runtime_issues"]:
                arcpy.AddWarning("")
                arcpy.AddWarning("Auffaelligkeiten / relevante Laufzeitprobleme:")
                for msg in result["runtime_issues"]:
                    arcpy.AddWarning(f"- {msg}")

            parameters[3].value = summary

        except Exception as e:
            arcpy.AddError(f"Tool-Ausfuehrung fehlgeschlagen: {e}")
            raise