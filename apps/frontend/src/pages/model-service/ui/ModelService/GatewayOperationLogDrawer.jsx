import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { CheckCircle2, Loader2, X, XCircle } from 'lucide-react';
import { cn } from '../../../../shared/lib/cn';
import { Button } from '../../../../shared/ui/Button';
import { Badge } from '../../../../shared/ui/Badge';
import { Checkbox } from '../../../../shared/ui/FormControls';
import { ToggleGroup } from '../../../../shared/ui/ToggleGroup';
import { formatTimestamp } from '../../../../shared/lib/formatTimestamp';

const LEVELS = { warning: 'warn', err: 'error', critical: 'fatal', panic: 'fatal' };
const PINO_LEVELS = { 10: 'trace', 20: 'debug', 30: 'info', 40: 'warn', 50: 'error', 60: 'fatal' };
const LEVEL_TONES = { trace: 'neutral', debug: 'neutral', info: 'info', warn: 'warning', error: 'danger', fatal: 'danger' };
const LOG_MODES = [{ value: 'readable', label: 'Readable' }, { value: 'raw', label: 'Raw' }];
const normalizeLevel = (value) => {
    const level = String(value).toLowerCase();
    return Object.hasOwn(LEVELS, level) ? LEVELS[level] : level;
};
const inlineLogValue = (value) => typeof value === 'string'
    ? value.replace(/\r/g, '\\r').replace(/\n/g, '\\n')
    : JSON.stringify(value);

function parseLogLine(raw) {
    let text = raw;
    const entry = { raw, message: raw, level: '', timestamp: '', source: null, fields: [], parseWarning: '' };
    const source = text.match(/^\[pod\/([^/\]\s]+)\/([^\]\s]+)\]\s*/);
    if (source) {
        entry.source = { pod: source[1], container: source[2] };
        text = text.slice(source[0].length);
    }
    const timestamp = text.match(/^(\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:\d{2}))\s+/);
    if (timestamp) {
        entry.timestamp = timestamp[1];
        text = text.slice(timestamp[0].length);
    }
    entry.message = text;
    const trimmed = text.trim();
    if (trimmed.startsWith('{')) {
        let record;
        try {
            record = JSON.parse(trimmed);
        } catch (error) {
            if (!(error instanceof SyntaxError)) throw error;
            entry.parseWarning = 'Could not parse structured log; showing the original text.';
            return entry;
        }
        if (!record || typeof record !== 'object' || Array.isArray(record)) return entry;
        const consumed = new Set();
        const take = (keys, valid) => {
            const key = keys.find((candidate) => Object.hasOwn(record, candidate) && valid(record[candidate]));
            if (!key) return undefined;
            consumed.add(key);
            return record[key];
        };
        const time = take(['timestamp', 'time', 'ts', '@timestamp', 'start_time'], (value) =>
            typeof value === 'string' && value.length > 0 || typeof value === 'number' && Number.isFinite(value));
        if (typeof time === 'number') {
            const date = new Date(Math.abs(time) < 1e11 ? time * 1000 : time);
            entry.timestamp = Number.isNaN(date.getTime()) ? String(time) : date.toISOString();
        } else if (time !== undefined) entry.timestamp = time;
        const level = take(['level', 'severity', 'severity_text', 'severityText', 'log.level'], (value) =>
            typeof value === 'string' && value.length > 0 || typeof value === 'number');
        if (level !== undefined) {
            entry.level = typeof record.level === 'number' && level === record.level
                ? PINO_LEVELS[level] || String(level)
                : normalizeLevel(level);
        }
        const message = take(['message', 'msg', 'body', 'log', 'event', 'error'], (value) =>
            typeof value === 'string' && value.length > 0);
        entry.message = message || '';
        if (!entry.message) {
            const method = record.method ?? record.request_method ?? record[':method'];
            const path = record.path ?? record.request_path ?? record[':path'];
            const status = record.response_code ?? record.status_code;
            if (typeof method === 'string' && typeof path === 'string') {
                entry.message = `${method} ${path}${typeof status === 'number' || typeof status === 'string' ? ` -> ${status}` : ''}`;
            }
        }
        entry.fields = Object.entries(record).filter(([key]) => !consumed.has(key));
        return entry;
    }
    const level = text.match(/^(?:\[(trace|debug|info|warn(?:ing)?|error|fatal|critical)\]|(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL))[:\s]+/i);
    if (level) {
        entry.level = normalizeLevel(level[1] || level[2]);
        entry.message = text.slice(level[0].length);
    }
    return entry;
}

function ComponentLogLine({ raw }) {
    const entry = useMemo(() => parseLogLine(raw), [raw]);
    const isError = entry.level === 'error' || entry.level === 'fatal';
    const [message, ...continuation] = isError ? entry.message.split(/\r\n|\r|\n/) : [entry.message];
    const stackFields = isError ? entry.fields.filter(([key, value]) =>
        /^(stack|stacktrace|stack_trace|exception\.stacktrace)$/i.test(key) && typeof value === 'string') : [];
    return (
        <li className="min-h-8 whitespace-nowrap px-3 py-1 font-mono text-[11px] leading-6">
            <div className="flex min-h-6 items-center gap-3">
                {entry.timestamp && <time className="shrink-0 tabular-nums text-theme-muted" title={String(entry.timestamp)}>
                    {formatTimestamp(entry.timestamp)}
                </time>}
                {entry.level && <Badge tone={Object.hasOwn(LEVEL_TONES, entry.level) ? LEVEL_TONES[entry.level] : 'neutral'} size="xxs" className="shrink-0">{entry.level.toUpperCase()}</Badge>}
                {entry.parseWarning && <Badge tone="warning" size="xxs" title={entry.parseWarning} className="shrink-0">Unparsed JSON</Badge>}
                {message && <span className="shrink-0 whitespace-pre text-theme-text">{inlineLogValue(message)}</span>}
                {entry.fields.filter((field) => !stackFields.includes(field)).map(([key, value]) => (
                    <span key={key} className="shrink-0 whitespace-pre text-theme-text">
                        <span className="text-theme-muted">{inlineLogValue(key)}=</span>{inlineLogValue(value)}
                    </span>
                ))}
                {entry.source && <span className="shrink-0 text-theme-muted">
                    source={entry.source.pod}/{entry.source.container}
                </span>}
                {!entry.message && entry.fields.length === 0 && <span className="whitespace-pre text-theme-muted">{inlineLogValue(entry.raw)}</span>}
            </div>
            {continuation.length > 0 && <pre className="whitespace-pre font-mono leading-5 text-theme-text">{continuation.join('\n')}</pre>}
            {stackFields.map(([key, value]) => (
                <pre key={key} className="mt-1 whitespace-pre font-mono leading-5 text-theme-text">
                    <span className="text-theme-muted">{key}</span>{'\n'}{value}
                </pre>
            ))}
        </li>
    );
}

export function ComponentLogViewer({ entries = [] }) {
    const [mode, setMode] = useState('readable');
    const [following, setFollowing] = useState(true);
    const viewportRef = useRef(null);
    useLayoutEffect(() => {
        if (following && viewportRef.current) {
            viewportRef.current.scrollTop = viewportRef.current.scrollHeight;
        }
    }, [entries, following, mode]);

    return (
        <div className="flex h-full min-h-0 flex-col gap-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-3">
                    <ToggleGroup options={LOG_MODES} value={mode} onChange={setMode} size="xs" />
                    <span className="text-[11px] text-theme-muted">{entries.length} / 500 lines</span>
                </div>
                <Checkbox label="Follow latest" checked={following} onChange={(event) => setFollowing(event.target.checked)} />
            </div>
            <div ref={viewportRef} role="log" aria-label="Component logs" aria-live="off" tabIndex={0}
                className="min-h-0 flex-1 overflow-auto rounded-xl border border-theme-border bg-theme-bg"
                onScroll={(event) => {
                    const node = event.currentTarget;
                    if (node.scrollHeight - node.scrollTop - node.clientHeight > 32) setFollowing(false);
                }}>
                {entries.length === 0 ? (
                    <p className="p-4 text-xs text-theme-muted">Waiting for log output...</p>
                ) : mode === 'raw' ? (
                    <pre className="whitespace-pre p-3 font-mono text-[11px] leading-relaxed text-theme-text">
                        {entries.map((entry) => entry.raw).join('\n')}
                    </pre>
                ) : (
                    <ol className="w-max min-w-full divide-y divide-theme-border">
                        {entries.map((entry) => <ComponentLogLine key={entry.id} raw={entry.raw} />)}
                    </ol>
                )}
            </div>
        </div>
    );
}

// Right-side, full-page-height log drawer for a data-plane operation. Streams
// the operation's log lines and final status.
export function GatewayOperationLogDrawer({ isOpen, onClose, title, status = 'running', lines = [], message = '' }) {
    const endRef = useRef(null);
    useEffect(() => {
        if (endRef.current) endRef.current.scrollIntoView({ block: 'end' });
    }, [lines.length, isOpen]);

    if (!isOpen || typeof document === 'undefined') return null;

    const icon = status === 'running'
        ? <Loader2 size={14} className="animate-spin text-sky-400" />
        : status === 'succeeded'
            ? <CheckCircle2 size={14} className="text-emerald-400" />
            : <XCircle size={14} className="text-rose-400" />;

    return createPortal(
        <>
            <button type="button" aria-label="Close log panel" className="fixed inset-0 z-[90] cursor-default bg-black/40" onClick={onClose} />
            <aside
                role="log"
                aria-live="polite"
                className="fixed inset-y-0 right-0 z-[100] flex h-full w-full max-w-xl flex-col border-l border-slate-700/70 bg-slate-950 shadow-2xl"
            >
                <header className="flex items-center gap-2 border-b border-slate-800 px-4 py-3">
                    {icon}
                    <span className="text-sm font-semibold text-slate-200">{title || 'Data-plane operation'}</span>
                    <span className={cn(
                        'rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
                        status === 'running' ? 'bg-sky-500/15 text-sky-300'
                            : status === 'succeeded' ? 'bg-emerald-500/15 text-emerald-300'
                                : 'bg-rose-500/15 text-rose-300',
                    )}>{status}</span>
                    <Button variant="ghost" size="icon" className="ml-auto" aria-label="Close" onClick={onClose}><X size={14} /></Button>
                </header>
                <div className="flex-1 overflow-y-auto bg-black/40 px-4 py-3 font-mono text-[11px] leading-relaxed text-slate-300">
                    {lines.length === 0 && <p className="text-slate-500">Starting…</p>}
                    {lines.map((line, index) => (
                        <div key={index} className="whitespace-pre-wrap break-words">{line}</div>
                    ))}
                    <div ref={endRef} />
                </div>
                {message && (
                    <footer className="border-t border-slate-800 px-4 py-3 text-xs text-slate-300">{message}</footer>
                )}
            </aside>
        </>,
        document.body,
    );
}

export default GatewayOperationLogDrawer;
