import { useState } from 'react';
import { defaultServing } from '@repo/shared/catalog';
import { Button, Input, Modal } from '../../../shared/ui';
import { FormError } from '../../../shared/ui/FormError';
import { saveCatalogModel, deleteCatalogModel } from '../../../shared/lib/useModelCatalog';
import { servingArguments } from '../../deployment/api/agenticDeploymentBackend';
import ModelParametersForm from './ModelParametersForm';

export default function CatalogModelEditor({ model, canDelete, onClose, onSaved }) {
    const editing = model?.source === 'custom';
    const [values, setValues] = useState({
        name: model ? `${model.name}${editing ? '' : ' (custom)'}` : '', repository: model?.repository || '',
        revision: model?.revision || 'main', modelType: model?.modelType || 'text-generation', dtype: model?.dtype || 'BF16',
        weightGiB: model?.weightGiB ?? '', contextLength: model?.contextLength ?? '',
    });
    const [serving, setServing] = useState(() => defaultServing(model));
    const [error, setError] = useState(''), [pending, setPending] = useState(false), [confirmDelete, setConfirmDelete] = useState(false);
    const submit = async event => {
        event.preventDefault(); setPending(true); setError('');
        try {
            servingArguments(serving);
            const saved = await saveCatalogModel(model, { ...values,
                weightGiB: values.weightGiB === '' ? null : Number(values.weightGiB),
                contextLength: values.contextLength === '' ? null : Number(values.contextLength), servingDefaults: serving,
            });
            onSaved(saved);
        } catch (failure) { setError(failure.message); }
        finally { setPending(false); }
    };
    const remove = async () => {
        setPending(true); setError('');
        try { await deleteCatalogModel(model); onSaved(null); }
        catch (failure) { setError(failure.message); }
        finally { setPending(false); }
    };
    const fields = [
        ['name', 'Display name'], ['repository', 'Hugging Face repository'], ['revision', 'Model revision'],
        ['modelType', 'Model type'], ['dtype', 'Weight precision'], ['weightGiB', 'Weight size (GiB)'], ['contextLength', 'Maximum context (tokens)'],
    ];
    return <Modal isOpen onClose={pending ? undefined : onClose} title={editing ? 'Edit model defaults' : 'Add custom model'} size="xl">
        <form onSubmit={submit} className="space-y-5">
            <fieldset disabled={pending} className="space-y-5">
                <div className="grid gap-4 sm:grid-cols-2">{fields.map(([key, label]) => <label key={key} className="text-xs font-medium text-theme-text">{label}<Input className="mt-1.5" value={values[key]} required={key !== 'contextLength'} type={['weightGiB', 'contextLength'].includes(key) ? 'number' : 'text'} min={key === 'contextLength' ? 256 : 0.001} step={key === 'weightGiB' ? 'any' : 1} pattern={key === 'repository' ? '[A-Za-z0-9_.\\-]+/[A-Za-z0-9_.\\-]+' : undefined} onChange={event => setValues(current => ({ ...current, [key]: event.target.value }))} /></label>)}</div>
                <p className="text-sm font-medium text-theme-text">Default model parameters</p>
                <ModelParametersForm value={serving} onChange={setServing} />
                <p className="text-xs text-theme-muted">Defaults apply to new deployments. Existing plans and deployments keep their saved configuration.</p>
                <FormError message={error} />
                {confirmDelete && <p role="alert" className="text-sm text-theme-text">Delete this catalog entry? Cached weights and existing deployments will be retained.</p>}
                <div className="flex flex-wrap justify-end gap-2">
                    {editing && canDelete && <Button type="button" variant="dangerOutline" onClick={() => confirmDelete ? remove() : setConfirmDelete(true)}>{confirmDelete ? 'Confirm deletion' : 'Delete model'}</Button>}
                    <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
                    <Button type="submit" isLoading={pending}>Save model</Button>
                </div>
            </fieldset>
        </form>
    </Modal>;
}
