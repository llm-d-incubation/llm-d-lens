// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Voice input for "Chat with Lens": fully client-side speech-to-text, no
// backend inference. Runs Whisper in the browser via transformers.js
// (WebAssembly + CPU) and fetches the model lazily through Lens' same-origin
// asset proxy, then lets the browser cache it for later turns.
//
// "onnx-community/whisper-base" (not the .en variant) is used deliberately:
//  - multilingual, since Prism's own operators have been chatting with Lens
//    in both English and Chinese in this session ("*.en" would only
//    understand English).
//  - the "onnx-community" org re-exports models specifically for this v3+
//    @huggingface/transformers pipeline (the older "Xenova/*" ONNX exports,
//    made for the predecessor @xenova/transformers v2, are missing some
//    metadata this library's ONNX Runtime Web build expects).
//
// whisper-tiny was too inaccurate for short Chinese utterances (it often
// hallucinated a single token); base is still small enough to run on CPU.
const MODEL_ID = 'onnx-community/whisper-base';
const SAMPLE_RATE = 16000;
const MIN_SPEECH_SECONDS = 0.15;
const MIN_RMS = 0.00005;
let transcriberPromise = null;

async function getTranscriber(onProgress) {
    if (!transcriberPromise) {
        transcriberPromise = import('@huggingface/transformers')
            .then(({ pipeline, env }) => {
                // Stream model files through Lens itself. Direct browser access
                // to huggingface.co fails on proxy-only networks, while the
                // Lens host already has outbound access for model downloads.
                env.remoteHost = `${window.location.origin}/api/v1/speech-models/`;
                env.remotePathTemplate = '{model}/resolve/{revision}/';
                return pipeline('automatic-speech-recognition', MODEL_ID, {
                    // NOT 'q8'/quantized: the merged-decoder quantized export
                    // for this model has a broken/incompatible scale tensor
                    // for its (embed_tokens-tied) MatMulNBits weight in this
                    // onnxruntime-web version, which fails session creation
                    // with "TransposeDQWeightsForMatMulNBits ... Missing
                    // required scale". fp32 sidesteps that broken graph path
                    // entirely.
                    dtype: 'fp32',
                    progress_callback: onProgress,
                });
            })
            .catch((error) => {
                // Don't cache a failed load -- let the next mic click retry
                // instead of permanently failing for the rest of the session.
                transcriberPromise = null;
                throw error;
            });
    }
    return transcriberPromise;
}

// Decodes a recorded audio Blob (whatever the browser's MediaRecorder
// produced, typically audio/webm;codecs=opus) into the mono 16kHz Float32
// PCM samples transformers.js' Whisper pipeline expects, using the Web
// Audio API's own decoder/resampler instead of pulling in an extra decoding
// dependency.
async function decodeToMono16k(blob) {
    const arrayBuffer = await blob.arrayBuffer();
    const AudioContextImpl = window.AudioContext || window.webkitAudioContext;
    const audioContext = new AudioContextImpl({ sampleRate: SAMPLE_RATE });
    try {
        const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);
        const channels = Math.max(1, audioBuffer.numberOfChannels);
        let loudest = audioBuffer.getChannelData(0);
        let loudestRms = 0;
        for (let channel = 0; channel < channels; channel += 1) {
            const data = audioBuffer.getChannelData(channel);
            let sum = 0;
            for (let index = 0; index < data.length; index += 1) sum += data[index] * data[index];
            const rms = Math.sqrt(sum / Math.max(1, data.length));
            if (rms > loudestRms) {
                loudest = data;
                loudestRms = rms;
            }
        }
        return new Float32Array(loudest);
    } finally {
        await audioContext.close();
    }
}

function trimSilence(samples) {
    const threshold = 0.001;
    let start = 0;
    let end = samples.length;
    while (start < end && Math.abs(samples[start]) < threshold) start += 1;
    while (end > start && Math.abs(samples[end - 1]) < threshold) end -= 1;
    if (end - start < SAMPLE_RATE * 0.15) return samples;
    const padding = Math.floor(SAMPLE_RATE * 0.12);
    return samples.slice(Math.max(0, start - padding), Math.min(samples.length, end + padding));
}

function speechStats(samples) {
    let sum = 0;
    for (let index = 0; index < samples.length; index += 1) sum += samples[index] * samples[index];
    return { duration: samples.length / SAMPLE_RATE, rms: Math.sqrt(sum / Math.max(1, samples.length)) };
}

// Lazily built Traditional -> Simplified Chinese converter (opencc-js).
// Whisper has no separate "script" control -- `language: 'chinese'` only
// picks Mandarin, not simplified vs. traditional characters -- and since its
// training data is mostly-but-not-exclusively simplified, occasional
// traditional characters slip into the output. Converting from 't'
// (traditional, superset) to 'cn' (simplified) is idempotent on text that's
// already simplified, so it's safe to always apply.
let zhConverterPromise = null;
function getZhConverter() {
    if (!zhConverterPromise) {
        zhConverterPromise = import('opencc-js').then(({ Converter }) => Converter({ from: 't', to: 'cn' }));
    }
    return zhConverterPromise;
}

// Transcribes one recorded utterance. `onModelProgress` (optional) receives
// transformers.js' download-progress events the first time it's called, so
// the caller can show "Downloading speech model..." only on that first use.
// `language` (optional, e.g. 'english'/'chinese') is forwarded straight to
// Whisper's decoder as a forced language hint: whisper-tiny's built-in
// language auto-detection is unreliable on short clips, so letting the user
// pick the language up front avoids it guessing wrong and transcribing
// gibberish in the wrong language.
export async function transcribeAudioBlob(blob, { onModelProgress, onModelReady, language } = {}) {
    const [transcriber, decoded] = await Promise.all([getTranscriber(onModelProgress), decodeToMono16k(blob)]);
    onModelReady?.();
    const audioData = trimSilence(decoded);
    const stats = speechStats(audioData);
    if (stats.duration < MIN_SPEECH_SECONDS || stats.rms < MIN_RMS) {
        throw new Error('No speech detected. Please try again and speak clearly into the microphone.');
    }
    let peak = 0;
    for (let index = 0; index < audioData.length; index += 1) peak = Math.max(peak, Math.abs(audioData[index]));
    if (peak > 0 && peak < 0.25) {
        const gain = 0.9 / peak;
        for (let index = 0; index < audioData.length; index += 1) audioData[index] *= gain;
    }
    const result = await transcriber(audioData, language ? { language, task: 'transcribe' } : undefined);
    const text = Array.isArray(result) ? result[0]?.text : result?.text;
    const trimmed = (text || '').trim();
    if (language === 'chinese' && trimmed) {
        const convert = await getZhConverter();
        return convert(trimmed);
    }
    return trimmed;
}

// Languages offered in the UI's voice-input language picker. Kept as a small
// fixed list (rather than Whisper's full ~100-language set) since these are
// the languages Prism operators have actually used with Chat with Lens.
export const SPEECH_TO_TEXT_LANGUAGES = [
    { value: 'english', label: 'English' },
    { value: 'chinese', label: 'Chinese' },
];

export function isSpeechToTextSupported() {
    return typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia && typeof window.MediaRecorder !== 'undefined';
}

// Browsers only expose navigator.mediaDevices.getUserMedia in a "secure
// context" (HTTPS, or http://localhost). When Prism is reached over plain
// HTTP on a non-localhost host/IP, mediaDevices is undefined and
// isSpeechToTextSupported() above returns false -- not because the browser
// lacks the feature, but because the page itself can't use it. Surfacing
// this distinction lets the UI explain *why* voice input is unavailable
// instead of silently hiding the button.
export function getMicUnavailableReason() {
    if (typeof window === 'undefined') return '';
    if (typeof window.MediaRecorder === 'undefined') {
        return 'This browser does not support audio recording (MediaRecorder API).';
    }
    if (!navigator.mediaDevices?.getUserMedia) {
        if (window.isSecureContext === false) {
            return 'Voice input requires HTTPS (or accessing Prism via http://localhost). Ask your admin to serve Prism over HTTPS, or use an SSH port-forward to localhost.';
        }
        return 'This browser does not support microphone access.';
    }
    return '';
}
