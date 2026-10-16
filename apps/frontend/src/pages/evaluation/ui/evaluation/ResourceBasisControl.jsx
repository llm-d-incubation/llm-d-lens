import { useEffect, useRef, useState } from 'react';
import { FileCode } from 'lucide-react';
import { Button } from '../../../../shared/ui/Button.jsx';
import { FormError } from '../../../../shared/ui/FormError.jsx';
import { recommendationBudget } from '../../model/experimentDesign.js';

export default function ResourceBasisControl({ onGenerate, hardware, disabled, loading = false, requiredCards = 1, label = 'Generate YAML', confirmLabel = 'Continue & generate' }) {
    const [open, setOpen] = useState(false);
    const root = useRef(null);
    const trigger = useRef(null);
    const dialog = useRef(null);
    const available = recommendationBudget(hardware, 'available');
    const total = recommendationBudget(hardware, 'total');
    const needsConfirmation = available < requiredCards;
    const exceedsTotal = total < requiredCards;
    const close = () => { setOpen(false); trigger.current?.focus(); };

    useEffect(() => {
        if (!open) return;
        dialog.current?.focus();
        const outside = event => { if (!root.current?.contains(event.target)) setOpen(false); };
        document.addEventListener('pointerdown', outside);
        return () => document.removeEventListener('pointerdown', outside);
    }, [open]);

    return <div ref={root} className="relative inline-flex" onKeyDown={event => {
        if (event.key === 'Escape' && open) { event.preventDefault(); close(); }
    }}>
        <Button ref={trigger} type="button" aria-haspopup={needsConfirmation ? 'dialog' : undefined} aria-expanded={open} disabled={disabled} isLoading={loading}
            onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                if (open) close();
                else if (needsConfirmation) setOpen(true);
                else onGenerate('available');
            }}>
            {!loading && <FileCode size={15} aria-hidden="true" />}
            {label}
        </Button>
        {open && <div ref={dialog} tabIndex={-1} role="dialog" aria-label="Insufficient available cards"
            className="absolute bottom-full right-0 z-50 mb-3 w-80 max-w-[calc(100vw-2rem)] rounded-xl border border-theme-border bg-theme-card p-4 shadow-2xl">
            <h3 className="mb-3 text-xs font-semibold text-theme-text">Card availability</h3>
            {needsConfirmation && <FormError message={`Insufficient available cards: this deployment requires ${requiredCards} cards, but only ${available} are currently available.`} />}
            <dl className="mt-3 space-y-2 text-xs text-theme-text">
                <div className="flex justify-between gap-3"><dt>Required per deployment</dt><dd>{requiredCards} cards</dd></div>
                <div className="flex justify-between gap-3"><dt>Currently available</dt><dd>{available} cards</dd></div>
                <div className="flex justify-between gap-3"><dt>All physical cards</dt><dd>{total} cards</dd></div>
            </dl>
            <div className="mt-3 text-xs leading-5 text-theme-muted">
                {needsConfirmation ? exceedsTotal
                    ? <FormError message="The required cards exceed the physical total. Reduce replicas / TP to continue." />
                    : <p>The physical total is sufficient. You can continue, but the deployment may remain Pending until enough cards become available.</p>
                    : <p>Enough cards are now available. You can continue.</p>}
                <p className="mt-2">Comparison targets run sequentially; keeping deployments can accumulate card usage.</p>
            </div>
            <div className="mt-4 flex items-center justify-end gap-3">
                <Button type="button" variant="ghost" size="xs" onClick={(event) => { event.preventDefault(); event.stopPropagation(); close(); }}>Cancel</Button>
                <Button type="button" size="xs" disabled={disabled || loading || (needsConfirmation && exceedsTotal)} onClick={(event) => {
                    event.preventDefault(); event.stopPropagation();
                    if (disabled || loading || (needsConfirmation && exceedsTotal)) return;
                    close(); onGenerate(needsConfirmation ? 'total' : 'available');
                }}>{confirmLabel}</Button>
            </div>
        </div>}
    </div>;
}
