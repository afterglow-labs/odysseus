"""Active sampling path of the PinkCherry publisher's hybrid v1.8 workflow."""
from scripts.h3_seed_hunter_presets import PINKCHERRY

SOURCE_REVISION = "08ac92dbd56a8523beea55aafaaba1a1d72c45cb"
SOURCE_URL = ("https://huggingface.co/SexGod1979/PinkCherry-NSFW_MiniMax-H3/blob/"
              f"{SOURCE_REVISION}/1.8-hybrid/PinkCherry_ref2va_turbo_lightx2v_workflow.json")

RECIPE = "pinkcherry"
LORA = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
COMPONENTS = {
    "model": PINKCHERRY, "lora": LORA,
    "encoder": "qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
}
DEFAULTS = {"recipe": RECIPE, "mode": "ref2va", "width": 672, "height": 1216,
            "frames": 294, "steps": 12, "seed": 916664778424829,
            "sampler": "euler", "scheduler": "simple", "shift_video": 1, "shift_audio": 1,
            "reference_size": "match"}
