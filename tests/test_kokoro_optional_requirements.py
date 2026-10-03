from pathlib import Path

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[1]


def test_local_speech_is_available_on_shared_python_without_skip_markers():
    requirements = [Requirement(line) for line in
                    (ROOT / "requirements-optional.txt").read_text().splitlines()
                    if line.strip() and not line.startswith("#")]
    speech = next(requirement for requirement in requirements if requirement.name == "sherpa-onnx")
    assert str(speech.specifier) == "==1.13.8"
    assert speech.marker is None
    assert not any(requirement.name == "kokoro" for requirement in requirements)
