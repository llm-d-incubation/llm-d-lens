import assert from 'node:assert/strict';
import test from 'node:test';
import {
    DRAFT_KEY,
    DRAFT_TTL_MS,
    MAX_PROMPT_LENGTH,
    consumeAssistantDraft,
    stageAssistantDraft,
} from './assistantHandoff.js';

class MemoryStorage {
    constructor() { this.values = new Map(); }
    getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
    setItem(key, value) { this.values.set(key, String(value)); }
    removeItem(key) { this.values.delete(key); }
}

test('Welcome handoff stores one draft, trims it and consumes it once', () => {
    globalThis.window = { sessionStorage: new MemoryStorage() };
    try {
        const draft = stageAssistantDraft({ prompt: '  Why is model-alpha degraded?  ', providerId: 'p-1' });
        assert.equal(draft.prompt, 'Why is model-alpha degraded?');
        assert.equal(draft.providerId, 'p-1');
        assert.ok(globalThis.window.sessionStorage.getItem(DRAFT_KEY));

        assert.deepEqual(consumeAssistantDraft(), {
            prompt: 'Why is model-alpha degraded?',
            providerId: 'p-1',
        });
        assert.equal(consumeAssistantDraft(), null);
    } finally {
        delete globalThis.window;
    }
});

test('expired, malformed and empty drafts are ignored', () => {
    const storage = new MemoryStorage();
    globalThis.window = { sessionStorage: storage };
    try {
        storage.setItem(DRAFT_KEY, JSON.stringify({
            prompt: 'old prompt',
            providerId: 'p-1',
            createdAt: Date.now() - DRAFT_TTL_MS - 1,
        }));
        assert.equal(consumeAssistantDraft(), null);

        storage.setItem(DRAFT_KEY, '{not-json');
        assert.equal(consumeAssistantDraft(), null);

        storage.setItem(DRAFT_KEY, JSON.stringify({ prompt: '   ', providerId: 'p-1', createdAt: Date.now() }));
        assert.equal(consumeAssistantDraft(), null);
    } finally {
        delete globalThis.window;
    }
});

test('oversized prompts are rejected before handoff', () => {
    globalThis.window = { sessionStorage: new MemoryStorage() };
    try {
        assert.throws(
            () => stageAssistantDraft({ prompt: 'x'.repeat(MAX_PROMPT_LENGTH + 1), providerId: 'p-1' }),
            /limited/,
        );
    } finally {
        delete globalThis.window;
    }
});
