"""Load the published H3 LightVAE Comfy checkpoint on our pinned core.

The checkpoint prunes whole encoder residual blocks. Absent blocks are
identities; a removed block that changes channels retains its shortcut.
This follows ModelTC/LightVAE's native Comfy adapter. The decoder's layer
count is already inferred from the checkpoint by the pinned core.
"""


def prune_encoder(model, state):
    from torch import nn

    class Shortcut(nn.Module):
        def __init__(self, projection):
            super().__init__()
            self.nin_shortcut = projection

        def forward(self, value):
            return self.nin_shortcut(value)

    for stage_index, stage in enumerate(model.encoder.down):
        for block_index, block in enumerate(stage.block):
            prefix = f"encoder.down.{stage_index}.block.{block_index}."
            if prefix + "conv1.weight" in state:
                continue
            if prefix + "nin_shortcut.weight" in state:
                stage.block[block_index] = Shortcut(block.nin_shortcut)
            else:
                stage.block[block_index] = nn.Identity()
    return model


def load_light_vae(loader, state, metadata, device):
    from comfy.ldm.minimax import vae as h3_vae
    original = h3_vae.MiniMaxH3VideoVAE
    # The pinned detector uses this block as a family marker. LightVAE removes
    # that block's convolutions. Supply the marker only for detection, then
    # remove it in the constructor before any state-dict loading can occur.
    marker = "encoder.down.5.block.0.conv1.weight"
    detected = dict(state)
    needs_marker = marker not in state
    if needs_marker:
        detected[marker] = state["encoder.down.5.block.1.conv1.weight"]

    def construct(*args, **kwargs):
        if needs_marker:
            detected.pop(marker, None)
        return prune_encoder(original(*args, **kwargs), state)

    # Each render has its own worker process, and loads VAEs synchronously.
    # Restore even on failure so a following ordinary VAE never gets pruned.
    h3_vae.MiniMaxH3VideoVAE = construct
    try:
        result = loader(sd=detected, metadata=metadata, device=device)
        result.throw_exception_if_invalid()
        return result
    finally:
        h3_vae.MiniMaxH3VideoVAE = original
