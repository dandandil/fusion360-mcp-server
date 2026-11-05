"""Working rules for agents: server instructions and the edit-model prompt.

Distilled from a real editing session (fusion-agent-notes.md): breakages
came from proposing changes before reading the original features, editing
on top instead of in place, and recomputing everything at once.
"""

INSTRUCTIONS = """\
Fusion 360 CAD tools. Units are centimeters. Each design-editing call is one \
undo step; a failed call is rolled back (rolled_back: true).

Editing an existing model:
1. Read before proposing: get_design_notes, get_timeline (problems_only), \
get_feature_info for the features of the area, get_dependents for what you \
plan to change.
2. Ask the user what the model does not say: assembly order, printing or \
manufacturing limits, fasteners, which outer surfaces may change. Store the \
answers with set_design_note.
3. Prefer editing the original features and parameters over adding new \
features on top.
4. Roll the marker to just after the edited feature (set_timeline_marker), \
then create_checkpoint and snapshot_references. Try risky edits with dry_run.
5. Move the marker forward in steps; after each step check get_timeline \
(problems_only) and check_references, and repair before moving on.
6. Recompute everything (compute_all) only at the end. Run slow calls with \
start_job and poll get_job.
7. If an attempt goes wrong: rollback_to_checkpoint.
"""

EDIT_MODEL_STEPS = (
    "get_design_notes",
    "get_timeline",
    "get_feature_info",
    "get_dependents",
    "set_design_note",
    "set_timeline_marker",
    "create_checkpoint",
    "snapshot_references",
    "dry_run",
    "check_references",
    "compute_all",
    "start_job",
    "get_job",
    "rollback_to_checkpoint",
)


def edit_model_prompt(task: str) -> str:
    return (
        f"Change an existing Fusion 360 model: {task}\n\n"
        "Do not propose a solution yet. First list the features, sketches and "
        "parameters that make up this area (get_timeline, get_feature_info, "
        "get_dependents) and ask me about assembly order, manufacturing and "
        "what may change. Then follow the server's editing rules:\n\n" + INSTRUCTIONS
    )
