# SFA named extractor v4

This is the current working extractor for the QQXJ/NextSoft resource archives.

## CN 2009 client resources

```powershell
cd <repo-or-working-copy>
python .\Res\sfa_named_extract_v4.py --root .\Res --out .\Res\extracted_named_v4 --runtime-image .\Res\TW.NClient.runtime_image_runasinvoker_15s.bin
```

Current verified result:

- Extracted files: `91612`
- Failed files: `0`
- Output tree: `Res\extracted_named_v4`
- Manifest: `Res\extracted_named_v4\sfa_named_manifest.csv`

## TW 2012 client resources

```powershell
cd <repo-or-working-copy>
python .\Res\sfa_named_extract_v4.py --root .\TW\Res --out .\Res\tw_extracted_named_v4 --runtime-image .\Res\TW.NClient.runtime_image_runasinvoker_15s.bin
```

## Notes

- The extractor preserves archive folder paths.
- It supports both known file-record sizes:
  - CN 2009: `276`
  - TW 2012: `292`
- It decodes the extra per-file payload layer used by `.luo`, `.xml`, and text-style files.
- Use `--raw-payloads` only if you need the still-encoded original payload bytes.
- `KFM` is a Gamebryo animation controller/manager file. It usually references a base model plus `KF` animation sequences.


