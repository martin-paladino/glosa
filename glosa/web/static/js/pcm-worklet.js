// AudioWorkletProcessor for the room station (Task 14a): converts the mic's
// audio to Int16 PCM and batches it into 100 ms (1600-sample) frames.
//
// Resampling to 16 kHz happens *before* this runs: station.js opens the
// AudioContext itself with `{ sampleRate: 16000 }`, so every block this
// processor sees already arrives at 16 kHz (the browser's own resampler
// does the work; nothing here assumes or checks a particular input rate).
//
// process() runs on the audio thread, once per 128-sample render quantum
// (8 ms at 16 kHz); it never allocates a growable buffer or does anything
// that could block, and posts a message to the main thread only once a
// full 100 ms frame is ready (globals.md: "chunks de 100 ms (3200 bytes),
// sin acumular más de ~100 ms" -- accumulating exactly one is the point).
class PCMFrames extends AudioWorkletProcessor {
  static FRAME_SAMPLES = 1600; // 100 ms @ 16 kHz mono s16le

  constructor() {
    super();
    this._pending = new Int16Array(0);
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) return true; // no input connected yet

    const incoming = new Int16Array(channel.length);
    for (let i = 0; i < channel.length; i++) {
      const s = Math.max(-1, Math.min(1, channel[i]));
      incoming[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }

    const merged = new Int16Array(this._pending.length + incoming.length);
    merged.set(this._pending);
    merged.set(incoming, this._pending.length);

    let offset = 0;
    while (merged.length - offset >= PCMFrames.FRAME_SAMPLES) {
      const frame = merged.slice(offset, offset + PCMFrames.FRAME_SAMPLES);
      this.port.postMessage(frame.buffer, [frame.buffer]);
      offset += PCMFrames.FRAME_SAMPLES;
    }
    this._pending = offset ? merged.slice(offset) : merged;

    return true; // keep the processor alive for the life of the station
  }
}

registerProcessor("pcm-frames", PCMFrames);
