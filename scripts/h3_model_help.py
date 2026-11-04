"""Small offline guides matched to installed checkpoint names, not UI defaults."""
from pathlib import Path


def model_documentation(name):
    name = Path(name).name
    example = ('Use <Picture 1> as the character reference. A small mint-green robot walks across a stone bridge at sunset. '
               'Keep its reference design and colors. The camera follows steadily from behind. '
               'Cinematic CG lighting, quiet footsteps and river ambience, no music.')
    result = {
        'title': name, 'summary': 'MiniMax H3 video/audio generator.',
        'usage': ['Describe the subject, action, setting, camera, visual style and desired sound. '
                  'Reference labels must match attached files: <Picture 1>, <Video 1>, <Audio 1>. '
                  'The example below requires one reference image.',
                  'Qwen encodes this text and visual references into numerical conditioning. It does not generate a hidden rewritten sentence. '
                  'Enhance prompt is a separate, optional rewrite that appears in your editable prompt box.'],
        'settings': ['Seed Hunter previews use Preview steps. If the selected preview is shorter than the final video, '
                     'Full-length generation steps controls a separate pass at preview resolution. '
                     'Refinement steps controls the subsequent pass after upscaling. Example: preview 8, generation 6, refinement 2.',
                     'No guide changes your draft or adds a Turbo LoRA automatically. Fewer steps can change quality and do not guarantee lower peak VRAM.'],
        'prompt_example': example, 'sources': [], 'caveats': [],
    }
    lower = name.casefold()
    if name == '10Eros_Max_h3_8stepTURBO_beta7_SingularityV2_int8HQ.safetensors':
        result['summary'] = 'Publisher merge of Singularity V2 and the 10Eros Max eight-step Turbo model. This is a complete checkpoint, not a LoRA.'
        result['settings'].insert(0, 'This file already includes the eight-step Turbo merge; a separate Turbo LoRA is not required simply to select eight steps.')
        result['caveats'] = ['The model card contains recipes for several different files. The newer PRISM DMAD recipe is not documented as a recipe for this beta7 file. Do not transfer its sampler and shift values automatically.']
        result['sources'] = [{'label': 'Publisher model card — check the exact beta7 filename', 'url': 'https://huggingface.co/OliviaRossi/Minimax-h3_Singularity-x-10Eros-Max_PRISM'}]
    elif lower.startswith('minimax-h3_singularity_ref2va_v2_'):
        result['summary'] = 'Singularity V2 is a Ref2VA H3 fine-tuned merge. The publisher reports improvements to action, prompt adherence, reference audio and visual styles.'
        result['usage'].insert(0, 'The V2 author recommends focused prompts under approximately 5,000 characters. Name the desired visual style and avoid repeated or conflicting instructions.')
        result['sources'] = [{'label': 'Singularity V2 README and prompting guide', 'url': 'https://huggingface.co/WarmBloodAban/Minimax-h3_Singularity'}]
        result['caveats'] = ['The optional four-step Turbo LoRA described in the README is a separate adapter. This guide does not require or enable it.']
    else:
        result['caveats'] = ['An exact publisher recipe has not been verified for this filename. The workflow explanations and example here describe Odysseus; they are not checkpoint-specific recommended settings.']
    return result
