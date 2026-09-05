from model import UserState


def build_planning_context(state: UserState) -> dict:

    context = {
        "energy": state.energy,
        "task_load": state.task_load,
        "external_pressure": state.external_pressure,
        "confidence": state.confidence,
    }

    if state.energy == "low":
        context["planning_mode"] = "protect_energy"

    elif state.task_load == "high":
        context["planning_mode"] = "reduce_load"

    elif state.external_pressure == "high":
        context["planning_mode"] = "protect_deadline"

    else:
        context["planning_mode"] = "normal"

    return context
