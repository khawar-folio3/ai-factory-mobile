// ocr <image.png> [--min 0.4] -> one line per recognised text: "x,y<TAB>text" (x,y = centre in image pixels, top-left origin)
// Local macOS Vision OCR; no network, no install. Built once into <factory home>/bin/ocr by platforms/android.py.
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1, let image = NSImage(contentsOfFile: args[1]),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("usage: ocr <image.png> [--min 0.4]\n".data(using: .utf8)!)
    exit(2)
}
let minConfidence = args.firstIndex(of: "--min").flatMap { Float(args[$0 + 1]) } ?? 0.4
let width = CGFloat(cg.width), height = CGFloat(cg.height)

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])

for case let obs as VNRecognizedTextObservation in request.results ?? [] {
    guard let best = obs.topCandidates(1).first, best.confidence >= minConfidence else { continue }
    let box = obs.boundingBox
    let x = Int((box.midX * width).rounded()), y = Int(((1 - box.midY) * height).rounded())
    print("\(x),\(y)\t\(best.string)")
}
