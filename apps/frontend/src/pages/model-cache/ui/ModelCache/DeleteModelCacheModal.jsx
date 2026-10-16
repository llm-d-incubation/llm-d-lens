import { useSubmission } from '../../../../shared/lib/useSubmission';
import { useEffect, useRef, useState } from 'react';
import { usePolling } from '../../../../shared/lib/usePolling';
import { getModelCacheEntry } from './modelCacheBackend';
import { AlertTriangle } from 'lucide-react';
import { DeleteConfirmation } from '../../../../shared/ui/DeleteConfirmation';
import { sourceDisplayName } from './modelCachePresentation';

export function DeleteModelCacheModal({ entry, onCancel, onDelete, onDeleted }) {
    const { pending: submitting, error, setError, run } = useSubmission('Failed to delete cached model');
    const [waiting, setWaiting] = useState(false);
    const deleting = submitting || waiting;
    const controllerRef = useRef(null);
    const confirmRef = useRef(null);

    useEffect(() => {
        confirmRef.current?.focus();
    }, []);

    useEffect(() => () => controllerRef.current?.abort(), []);

    usePolling(async () => {
        const controller = new AbortController();
        controllerRef.current = controller;
        try {
            let current;
            try {
                current = await getModelCacheEntry(entry.id, { signal: controller.signal });
            } catch (failure) {
                if (failure.status !== 404) throw failure;
            }
            if (controller.signal.aborted) return;
            if (!current || current.status === 'deleted') {
                await onDeleted();
                return;
            }
            if (current.status !== 'deleting') {
                throw new Error(current.failureDetail || 'Model cache deletion did not complete. Please try again.');
            }
        } catch (failure) {
            if (controller.signal.aborted) return;
            setWaiting(false);
            setError(failure.message || 'Failed to check model cache deletion');
        }
    }, { enabled: waiting, intervalMs: 2000 });

    if (!entry) return null;

    const submit = async (event) => {
        event.preventDefault();
        if (deleting) return;
        await run(async () => {
            const updated = await onDelete();
            if (updated.status === 'deleted') {
                await onDeleted();
            } else {
                setWaiting(true);
            }
        });
    };

    return (
        <DeleteConfirmation
            onCancel={onCancel}
            pending={deleting}
            error={error}
            title="Delete cached model"
            subtitle={sourceDisplayName(entry)}
            formId="delete-model-cache-form"
            onSubmit={submit}
            confirmLabel={deleting ? 'Deleting' : 'Delete cached model'}
            confirmRef={confirmRef}
        >
            {deleting && <p role="status" className="text-sm text-theme-muted">
                Deleting model files. This dialog will close when deletion is confirmed.
            </p>}
            <div className="flex gap-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2.5">
                <AlertTriangle size={16} className="mt-0.5 shrink-0 text-rose-300" aria-hidden="true" />
                <p className="text-xs leading-relaxed text-rose-100">
                    This removes only this model&apos;s files (<code className="rounded bg-rose-950/40 px-1 py-0.5">{entry.cachePath}</code>) from the
                    storage volume. Other cached models on the same volume are not affected, and the storage
                    volume itself is not deleted.
                </p>
            </div>
        </DeleteConfirmation>
    );
}

export default DeleteModelCacheModal;
