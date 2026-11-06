"""Working rules for agents: server instructions and the edit-model prompt.

Distilled from a real editing session (fusion-agent-notes.md): breakages
came from proposing changes before reading the original features, editing
on top instead of in place, and recomputing everything at once.
"""

INSTRUCTIONS = """\
Fusion 360 CAD tools. Units are centimeters. Each design-editing call is one \
undo step; a failed call is rolled back (rolled_back: true).

Read these fields in every result:
- state: running + job_id — the call outlived the 30 s wait and goes on in \
Fusion; poll get_job, never repeat the call.
- timeline_changes.removed_items — Fusion deleted these items along with your \
edit (cascade); undo unless that was intended. new_problems — items that \
started failing.
- user_actions — commands the user ran in Fusion since your last call; re-read \
the affected area before continuing.

Editing an existing model:
1. Read before proposing: get_design_notes, get_timeline (problems_only), \
features_near to find what makes up the area, get_feature_info for those \
features, get_dependents for what you plan to change. Look at the area with \
render_view focus (a sketch shows its dimensions); avoid dumping the whole \
model.
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
    "features_near",
    "render_view",
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
