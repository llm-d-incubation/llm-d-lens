import { useEffect, useState } from 'react';
import { evaluationApi } from '../../features/evaluation/client';
import { TERMINAL_EVALUATION_STATUSES } from '../../features/evaluation/domain';
import { usePolling } from '../../hooks/usePolling';
import { benchmarkTimeDisplay, formatBenchmarkMinutes as minutes } from '../../features/evaluation/benchmarkSettings';
import { Badge } from '../ui/Badge';

export default function BenchmarkTiming({ benchmark, run, enabled = true, compact = false }) {
    const [preview, setPreview] = useState(null);
    const [now, setNow] = useState(Date.now);
    const payload = run || !enabled ? null : JSON.stringify(benchmark);
    useEffect(() => {
        if (!payload) return undefined;
        const controller = new AbortController();
        const timer = setTimeout(async () => {
            try {
                const timing = await evaluationApi('/api/v1/evaluate/timing-estimate', {
                    method: 'POST', body: payload, signal: controller.signal,
                });
                if (!controller.signal.aborted) setPreview({ payload, timing });
            } catch {
                if (!controller.signal.aborted) setPreview({ payload, error: true });
            }
        }, 300);
        return () => { clearTimeout(timer); controller.abort(); };
    }, [payload]);
    usePolling(() => setNow(Date.now()), { enabled: Boolean(run && !TERMINAL_EVALUATION_STATUSES.has(run.status)), immediate: true });
    const timing = run?.timing || (preview?.payload === payload ? preview?.timing : null);
    if (!run && !timing) return <p className="mt-3 text-sm text-theme-muted">{!enabled ? 'Estimate pending valid settings' : preview?.payload === payload && preview?.error ? 'Time estimate unavailable' : 'Estimating benchmark time...'}</p>;
    const display = benchmarkTimeDisplay(run || { timing }, now);
    if (compact && run) {
        if (TERMINAL_EVALUATION_STATUSES.has(run.status)) return null;
        return <Badge size="md" tone="info" className="max-w-full whitespace-normal" aria-label="Estimated benchmark time remaining" title={run.id}>
            {display.remaining}
        </Badge>;
    }
    return <section aria-label="Benchmark time estimate" className="min-w-0 border-t border-theme-border py-3 text-sm text-theme-text">
        <dl className="flex flex-wrap gap-x-8 gap-y-3">
            <div><dt className="text-xs text-theme-muted">Estimated benchmark time / target</dt><dd>{display.estimate}</dd></div>
            {timing?.measured_requests != null && <div><dt className="text-xs text-theme-muted">Measured requests / target</dt><dd>{timing.measured_requests.toLocaleString()}</dd></div>}
            {(timing?.timeout_seconds ?? run?.wait_timeout_seconds) > 0 && <div><dt className="text-xs text-theme-muted">{timing?.timeout_mode === 'auto' ? 'Automatic' : 'Manual'} timeout / invocation</dt><dd>{minutes(timing?.timeout_seconds ?? run.wait_timeout_seconds)}</dd></div>}
        </dl>
        {run && <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1"><span>{display.remaining}</span>{display.elapsed && <span>Elapsed: {display.elapsed}</span>}{display.deadline && <span>{display.deadline}</span>}</div>}
        {timing?.assumptions && <p className="mt-2 max-w-4xl text-xs text-theme-muted">{timing.assumptions}</p>}
        {timing?.timeout_warning && <p role="alert" className="mt-2 text-sm text-theme-text">{timing.timeout_warning}</p>}
    </section>;
}