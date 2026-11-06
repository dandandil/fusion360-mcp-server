"""Working rules for agents: server instructions and the edit-model prompt.

Distilled from a real editing session (fusion-agent-notes.md): breakages
came from proposing changes before reading the original features, editing
on top instead of in place, and recomputing everything at once.
"""

INSTRUCTIONS = """\
Fusion 360 CAD tools. Lengths in results are centimeters; pass dimensions as \
expressions with units ("28 mm") wherever a tool takes an expression. Each \
design-editing call is one undo step; a failed call is rolled back \
(rolled_back: true).

Read these fields in every result:
- state: running + job_id — the call outlived the 30 s wait and goes on in \
Fusion; get_job with wait_s, never repeat the call.
- timeline_changes.removed_items — Fusion deleted these items along with your \
edit (cascade); undo unless that was intended. new_problems — items that \
started failing.
- user_actions — commands the user ran in Fusion since your last call; re-read \
the affected area before continuing.

Editing an existing model:
1. Read before proposing: get_design_notes, get_timeline (problems_only), \
features_near to find what makes up the area, get_feature_info and \
get_sketch_info for those items. Look with render_view focus or center + \
radius. Avoid dumping the whole model.
2. Ask the user what the model does not say: assembly order, printing or \
manufacturing limits, fasteners, which outer surfaces may change. Store the \
answers with set_design_note.
3. Prefer editing the original features and parameters over adding new \
features on top; recreate_feature when the API cannot change an input.
4. create_checkpoint, then roll the marker to just after the edited feature \
(set_timeline_marker). Checkpoints survive marker moves.
5. Make the edit, then walk forward with advance_marker (start_job): it \
stops at the first failing item. Repair it and advance again.
6. Finish with one compute_all (it can catch failures the walk missed).
7. If an attempt goes wrong: rollback_to_checkpoint.

Optional and slow on big models: get_dependents (cached after the first \
call), dry_run.
"""

EDIT_MODEL_STEPS = (
    "get_design_notes",
    "get_timeline",
    "get_feature_info",
    "get_sketch_info",
    "features_near",
    "render_view",
    "set_design_note",
    "recreate_feature",
    "create_checkpoint",
    "set_timeline_marker",
    "advance_marker",
    "start_job",
    "get_job",
    "compute_all",
    "rollback_to_checkpoint",
)


def edit_model_prompt(task: str) -> str:
    return (
        f"Change an existing Fusion 360 model: {task}\n\n"
        "Do not propose a solution yet. First list the features, sketches and "
        "parameters that make up this area (features_near, get_feature_info, "
        "get_sketch_info) and ask me about assembly order, manufacturing and "
        "what may change. Then follow the server's editing rules:\n\n" + INSTRUCTIONS
    )
