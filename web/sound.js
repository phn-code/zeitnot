// Recorded samples from web/sounds (CC0, see LICENSE.md there), played through one small mix:
// voices -> bus -> (dry + short synthetic room) -> gentle compressor -> master gain -> speakers.

const RATE_JITTER = 0.03;
const GAIN_JITTER_DB = 1.5;
const ROOM_SECONDS = 0.35;
const ROOM_WET = 0.1;

const jitter = (amount) => (Math.random() * 2 - 1) * amount;

/** Stereo noise with an exponential decay: a small, soft room. */
function roomImpulse(ctx) {
  const length = Math.round(ROOM_SECONDS * ctx.sampleRate);
  const impulse = ctx.createBuffer(2, length, ctx.sampleRate);
  for (let channel = 0; channel < 2; channel++) {
    const data = impulse.getChannelData(channel);
    for (let i = 0; i < length; i++) data[i] = (Math.random() * 2 - 1) * Math.exp((-6 * i) / length);
  }
  return impulse;
}

export class Sound {
  constructor({ enabled, volume }) {
    this.enabled = enabled;
    this.volume = volume;
    this.ctx = null;
    this.takes = new Map(); // event -> AudioBuffer[]
    this.lastTake = new Map(); // event -> index of the take played last
    this.loaded = null;
  }

  /** Browsers block audio until a user gesture, so the context and the samples load on the first one. */
  unlock() {
    if (this.ctx) {
      if (this.ctx.state === "suspended") this.ctx.resume();
      return;
    }
    const ctx = new AudioContext();
    this.ctx = ctx;
    this.master = new GainNode(ctx, { gain: this.volume });
    const compressor = new DynamicsCompressorNode(ctx, { threshold: -18, knee: 12, ratio: 3, attack: 0.003, release: 0.15 });
    this.bus = new GainNode(ctx);
    const room = new ConvolverNode(ctx, { buffer: roomImpulse(ctx) });
    const tone = new BiquadFilterNode(ctx, { type: "lowpass", frequency: 6000 });
    this.bus.connect(new GainNode(ctx, { gain: 1 - ROOM_WET })).connect(compressor);
    this.bus.connect(room).connect(tone).connect(new GainNode(ctx, { gain: ROOM_WET })).connect(compressor);
    compressor.connect(this.master).connect(ctx.destination);
    this.loaded = this.load().catch((err) => console.warn("sounds failed to load", err));
  }

  async load() {
    const manifest = await (await fetch("sounds/manifest.json")).json();
    const decode = async (file) => this.ctx.decodeAudioData(await (await fetch(`sounds/${file}`)).arrayBuffer());
    await Promise.all(
      Object.entries(manifest).map(async ([event, files]) => this.takes.set(event, await Promise.all(files.map(decode)))),
    );
  }

  setEnabled(enabled) {
    this.enabled = enabled;
  }

  setVolume(volume) {
    this.volume = volume;
    this.master?.gain.setTargetAtTime(volume, this.ctx.currentTime, 0.02);
  }

  play(event, delayMs = 0) {
    if (!this.enabled || !this.loaded) return;
    const requested = this.ctx.currentTime;
    this.loaded.then(() => this.voice(event, Math.max(this.ctx.currentTime, requested + delayMs / 1000)));
  }

  voice(event, when) {
    const takes = this.takes.get(event);
    if (!takes?.length) return;
    const last = this.lastTake.get(event);
    let index = Math.floor(Math.random() * takes.length);
    if (takes.length > 1 && index === last) index = (index + 1 + Math.floor(Math.random() * (takes.length - 1))) % takes.length;
    this.lastTake.set(event, index);
    const source = new AudioBufferSourceNode(this.ctx, { buffer: takes[index], playbackRate: 1 + jitter(RATE_JITTER) });
    source.connect(new GainNode(this.ctx, { gain: 10 ** (jitter(GAIN_JITTER_DB) / 20) })).connect(this.bus);
    source.start(when);
  }
}
