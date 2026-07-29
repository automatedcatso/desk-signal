# Local image OCR

Image extraction prefers the `shared/offline_ocr.py` helper with RapidOCR and
ONNX Runtime. If those optional local packages are absent, the engine falls
back to other installed extractors and reports OCR as unavailable without
failing the rest of the evidence pipeline.
