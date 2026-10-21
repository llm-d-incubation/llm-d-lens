const DRAFT_KEY = 'prism_welcome_assistant_draft';
const DRAFT_TTL_MS = 5 * 60 * 1000;
const MAX_PROMPT_LENGTH = 4000;

export function stageAssistantDraft({ prompt, providerId }) {
    const text = String(prompt || '').trim();
    if (!text) throw new Error('Enter a message for Lens Assistant.');
    if (text.length > MAX_PROMPT_LENGTH) throw new Error(`Messages are limited to ${MAX_PROMPT_LENGTH} characters.`);
    const draft = { prompt: text, providerId: providerId || null, createdAt: Date.now() };
    window.sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
    return draft;
}

export function consumeAssistantDraft() {
    let raw;
    try {
        raw = window.sessionStorage.getItem(DRAFT_KEY);
    } catch {
        return null;
    }
    if (!raw) return null;
    try {
        window.sessionStorage.removeItem(DRAFT_KEY);
    } catch {
        // A read-only storage failure must not break the assistant page.
    }
    let draft;
    try {
        draft = JSON.parse(raw);
    } catch {
        return null;
    }
    if (!draft || typeof draft.prompt !== 'string' || !draft.prompt.trim()) return null;
    if (!Number.isFinite(draft.createdAt) || Date.now() - draft.createdAt > DRAFT_TTL_MS) return null;
    return {
        prompt: draft.prompt.slice(0, MAX_PROMPT_LENGTH),
        providerId: typeof draft.providerId === 'string' ? draft.providerId : null,
    };
}

export { DRAFT_KEY, DRAFT_TTL_MS, MAX_PROMPT_LENGTH };
