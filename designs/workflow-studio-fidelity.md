# Implementation review against the approved hybrid concept

Reference: `odysseus-workflow-pipeline-loras.png`, `odysseus-workflow-node-detail.png`, and the interactive comparison HTML.

| Point | Approved concept | Production implementation |
| --- | --- | --- |
| Workspace hierarchy | Stage navigation, large center, separate inspector | Same desktop arrangement; mobile stacks the inspector below a horizontal stage strip. |
| Model connection | Base H3 → named adapter stack → Generate | Visible in Pipeline; expanded graph connects model to adapter stack before the bundle enters Generate. |
| Canvas density | Compact operation nodes, no embedded textareas | Fixed compact nodes; original prompt, component and numeric controls live in the inspector. |
| Wiring | Orthogonal routes and grouped setup | Short right-angle routes; collapsed setup hub; one thick setup connection enters Generate. |
| Theme | Afterglow purple surfaces with orange actions | Uses existing application theme variables; no separate theme override imposed. |
| Shared draft | View switches preserve parameters and selection | Both views move and reuse the original live forms. Browser checks verify identical prompt/form objects and preserved LoRA strengths. |
| Batch/history | Repeated work stays out of the graph | Existing batch controls and paginated queue remain within Inputs/Review; no per-video node duplication. |
| Source preview | Large center preview | Real files and authenticated E/F server sources play on demand. Illustrative mountains and simulated timeline were removed. |

Intentional differences: production runs inside the existing Close-only workflow dialog, not the standalone comparison page. Jobs are shown in Review rather than a permanently repeated bottom table. Refine appears only for Seed Hunter, which actually supports latent refinement. The implemented map edits supported native recipes; arbitrary rewiring and synthetic comparison branches from the concept are not represented as working features. The draft's input-selection controls retain their existing reference ranges and per-clip length behavior.

Copy changes: concept/demo labels, sample names, fake queue states and simulated controls are omitted. Buttons identify actual operations: Add to queue, Import ComfyUI, Apply to draft, Expand setup hub, and the existing workflow export actions. GPU selectors and output duration use full-width rows in the inspector so their labels remain readable.

Validation uses synthetic media, components and jobs. Browser QA covers H3, Seed Hunter and BFS; node/pipeline parity; native form validation; queue editing and pagination; explicit-close behavior; narrow screens; server preview requests; and conversion apply/block behavior. No private job media or prompts are used in screenshots.
