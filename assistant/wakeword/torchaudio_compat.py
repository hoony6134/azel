"""Restore torchaudio APIs that openWakeWord's training stack still calls but torchaudio 2.10
removed (decoding moved to torchcodec). Loaded at interpreter start via zz_torchaudio_compat.pth."""
import types

import soundfile as sf
import torchaudio


def _info(path, *args, **kwargs):
    i = sf.info(str(path))
    return types.SimpleNamespace(sample_rate=i.samplerate, num_frames=i.frames, num_channels=i.channels,
                                 bits_per_sample=16, encoding="PCM_S")


def _load(path, frame_offset=0, num_frames=-1, normalize=True, channels_first=True, *args, **kwargs):
    import torch
    data, sr = sf.read(str(path), start=frame_offset, stop=None if num_frames < 0 else frame_offset + num_frames,
                       dtype="float32", always_2d=True)
    t = torch.from_numpy(data)
    return (t.T.contiguous() if channels_first else t), sr


def _save(path, src, sample_rate, channels_first=True, *args, **kwargs):
    data = src.detach().cpu().numpy()
    sf.write(str(path), data.T if channels_first else data, sample_rate, subtype="PCM_16")


# load/save are replaced even though they exist: on this aarch64 build their torchcodec backend
# fails to load, and every file in the wake-word pipeline is a plain WAV that soundfile handles.
torchaudio.load, torchaudio.save = _load, _save

for _name, _value in {"list_audio_backends": lambda: ["soundfile"],
                      "set_audio_backend": lambda *a, **k: None,
                      "info": _info}.items():
    if not hasattr(torchaudio, _name):
        setattr(torchaudio, _name, _value)
