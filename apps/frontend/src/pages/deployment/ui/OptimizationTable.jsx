import { useState } from 'react';
import { Maximize2, Settings2, Sparkles } from 'lucide-react';
import { Badge, Button, Spinner } from '../../../shared/ui';
import { optimizationRows } from '../model/deploymentPlan';

// Checked but not established by this plan; the reason is shown on the row.
const NOT_APPLIED = { 'Auto: off': 'Not needed', 'Not selected': 'Not selected', Degraded: 'Not applied', 'Checks pending': 'Checks pending' };

/** Every optimization of the plan in one list: check to enable, settings to adjust parameters. */
export function OptimizationTable({ plan, candidate, mode = 'manual', disabled = false, compact = false, onOpen, onToggle, onExpand }) {
    const [pending, setPending] = useState(''), [error, setError] = useState('');
    const rows = optimizationRows(plan, candidate, mode);
    const toggle = async (row, enabled) => {
        setPending(row.id); setError('');
        try { if (!await onToggle(row.id, enabled)) setError('The plan could not be rebuilt. Your previous plan is unchanged; see the planning error.'); }
        catch (failure) { setError(failure.message); }
        finally { setPending(''); }
    };
    const settings = row => <Button size="icon" variant="ghost" disabled={disabled} title={`Adjust ${row.label}`} aria-label={`Adjust ${row.label}`} onClick={() => onOpen(row.id)}><Settings2 size={14} /></Button>;
    const checkbox = row => pending === row.id ? <Spinner size="xs" />
        : <input type="checkbox" className="h-4 w-4 cursor-pointer accent-[var(--brand-accent)] disabled:cursor-not-allowed" checked={row.checked} aria-label={`${row.checked ? 'Disable' : 'Enable'} ${row.label}`}
            title={row.toggleable ? undefined : 'Controlled by source settings; use the settings button.'}
            disabled={disabled || !onToggle || !row.toggleable || Boolean(pending)} onChange={event => toggle(row, event.target.checked)} />;
    const enabledCount = rows.filter(row => row.state === 'Enabled').length;
    const dot = row => <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-full" style={{ background: row.state === 'Enabled' ? '#10b981' : NOT_APPLIED[row.state] ? '#f59e0b' : 'var(--text-muted)' }} />;
    if (compact) return <section aria-label="Plan optimizations" className="flex max-h-full w-full flex-col self-start overflow-hidden rounded-xl border border-theme-border bg-theme-card shadow-lg ring-1 ring-[color-mix(in_srgb,var(--brand-accent)_25%,transparent)]">
        <div className="flex shrink-0 items-center gap-2 border-b border-theme-border bg-[color-mix(in_srgb,var(--brand-accent)_10%,transparent)] px-3 py-1.5">
            <Sparkles size={14} className="text-[var(--brand-accent)]" aria-hidden="true" />
            <h3 className="text-sm font-semibold">Optimizations</h3>
            <span className="text-xs tabular-nums text-theme-muted">{enabledCount}/{rows.length}</span>
            {onExpand && <Button size="icon" variant="ghost" className="ml-auto h-7 w-7" title="Show optimizations in the center" aria-label="Show optimizations in the center" onClick={onExpand}><Maximize2 size={13} /></Button>}
        </div>
        <ul className="min-h-0 flex-1 overflow-y-auto py-1">{rows.map(row => <li key={row.id} className="flex items-center gap-2 px-3 py-px hover:bg-theme-bg/60">
            {dot(row)}
            <span className={`min-w-0 flex-1 truncate text-[13px] ${row.state === 'Enabled' ? '' : 'text-theme-muted'}`} title={NOT_APPLIED[row.state] ? `${row.label}: ${row.reason}` : row.detail || row.label}>{row.label}</span>
            <Button size="icon" variant="ghost" className="h-6 w-6" disabled={disabled} title={`Adjust ${row.label}`} aria-label={`Adjust ${row.label}`} onClick={() => onOpen(row.id)}><Settings2 size={13} /></Button>
        </li>)}</ul>
        {error && <p role="alert" className="px-3 py-1.5 text-xs">{error}</p>}
    </section>;
    return <section aria-label="Plan optimizations" className="space-y-3">
        <p className="text-sm text-theme-muted">{enabledCount} of {rows.length} optimizations enabled. Check or uncheck to enable or disable one; the plan is rebuilt and rechecked. Use the settings button to adjust parameters.</p>
        <div className="overflow-x-auto rounded-xl border border-theme-border bg-theme-card">
            <table className="w-full min-w-[36rem] text-left text-sm">
                <thead className="border-b border-theme-border bg-[color-mix(in_srgb,var(--brand-accent)_8%,transparent)] text-xs text-theme-muted">
                    <tr><th scope="col" className="w-10 py-2.5 pl-4"><span className="sr-only">Enabled</span></th><th scope="col" className="px-3 py-2.5 font-medium">Optimization</th><th scope="col" className="px-3 py-2.5 font-medium">Parameters</th><th scope="col" className="w-12 px-2"><span className="sr-only">Settings</span></th></tr>
                </thead>
                <tbody className="divide-y divide-theme-border">{rows.map(row => {
                    const note = NOT_APPLIED[row.state];
                    return <tr key={row.id} className={`align-top transition-colors hover:bg-theme-bg/50 ${row.state === 'Enabled' ? 'border-l-2 border-l-[var(--brand-accent)]' : 'border-l-2 border-l-transparent'}`}>
                        <td className="py-3 pl-4">{checkbox(row)}</td>
                        <td className="px-3 py-3">
                            <span className={`flex flex-wrap items-center gap-2 font-medium ${row.state === 'Enabled' ? '' : 'text-theme-muted'}`}>{row.label}{note && <Badge size="xs" tone={row.tone}>{note}</Badge>}</span>
                            {note && row.reason ? <p className="mt-1 max-w-xl text-xs leading-relaxed text-theme-muted">{row.reason}</p>
                                : row.detail && <p className="mt-1 max-w-xl text-xs leading-relaxed text-theme-muted">{row.detail}</p>}
                        </td>
                        <td className="px-3 py-3 text-xs">{row.parameters.length ? <span className="flex flex-wrap gap-1.5">{row.parameters.map(([key, value]) => <span key={key} className="rounded-md border border-theme-border bg-theme-bg px-2 py-0.5"><span className="text-theme-muted">{key}</span> {value}</span>)}</span> : <span className="text-theme-muted">—</span>}</td>
                        <td className="px-2 py-2.5 text-right">{settings(row)}</td>
                    </tr>;
                })}</tbody>
            </table>
        </div>
        {error && <p role="alert" className="text-sm">{error}</p>}
    </section>;
}
