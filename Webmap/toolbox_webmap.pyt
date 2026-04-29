import arcpy


class Toolbox(object):
    def __init__(self):
        self.label = "WebMap Layer Replace Toolbox"
        self.alias = "webmapreplace"
        self.tools = [ReplaceWebMapLayersTool]


class ReplaceWebMapLayersTool(object):
    def __init__(self):
        self.label = "Layer in WebMaps ersetzen"
        self.description = (
            "Ersetzt in WebMaps veraltete Layer kntrolliert "
            "durch einen neuen Ziel-Layer."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        param_old_id = arcpy.Parameter(
            displayName="Alte Layer-ID",
            name="old_layer_id",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )

        param_new_id = arcpy.Parameter(
            displayName="Neue Layer-ID",s
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

        return [param_old_id, param_new_id, param_dry_run, param_summary]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        if not parameters[2].altered:
            parameters[2].value = False
        return

    def updateMessages(self, parameters):
        old_id = parameters[0].valueAsText
        new_id = parameters[1].valueAsText

        if not old_id or not old_id.strip():
            parameters[0].setErrorMessage("Eine alte Layer-ID ist erforderlich.")
        elif len(old_id.strip()) != 32:
            parameters[0].setWarningMessage(
                "Die alte Layer-ID hat nicht die erwartete Laenge von 32 Zeichen."
            )

        if new_id:
            if len(new_id.strip()) != 32:
                parameters[1].setWarningMessage(
                    "Die neue Layer-ID hat nicht die erwartete Laenge von 32 Zeichen."
                )
            elif old_id and new_id.strip() == old_id.strip():
                parameters[1].setWarningMessage(
                    "Die neue Layer-ID ist identisch mit der alten Layer-ID."
                )

        return

    def execute(self, parameters, messages):
        try:
            from replace_webmap_layers import run_layer_replacement
        except Exception as e:
            arcpy.AddError(f"Import von replace_webmap_layers.py fehlgeschlagen: {e}")
            raise

        old_layer_id = parameters[0].valueAsText.strip()
        new_layer_id = parameters[1].valueAsText.strip()
        dry_run = bool(parameters[2].value) if parameters[2].value is not None else False

        try:
            result = run_layer_replacement(
                old_layer_itemid=old_layer_id,
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
