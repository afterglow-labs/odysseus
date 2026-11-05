from scripts.h3_model_help import model_documentation


def test_exact_checkpoint_guides_do_not_change_execution_settings():
    guide = model_documentation('10Eros_Max_h3_8stepTURBO_beta7_SingularityV2_int8HQ.safetensors')
    assert 'not a LoRA' in guide['summary']
    assert 'beta7' in guide['caveats'][0]
    assert 'newer PRISM' in guide['caveats'][0]
    assert guide['prompt_example'] and guide['sources']
    assert 'recommended_settings' not in guide
    assert not model_documentation('unknown_ref2va.safetensors')['sources']
    assert '5,000' in model_documentation('Minimax-h3_Singularity_ref2va_v2_pruned_int8.safetensors')['usage'][0]
