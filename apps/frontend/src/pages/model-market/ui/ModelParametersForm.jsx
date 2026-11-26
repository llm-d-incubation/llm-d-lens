import { Checkbox, Input, Textarea } from '../../../shared/ui';

export default function ModelParametersForm({ value, onChange }) {
    const set = (key, next) => onChange({ ...value, [key]: next });
    return <div className="space-y-4">
        <Checkbox label="Enable tool calling" checked={value.toolCalling} onChange={event => set('toolCalling', event.target.checked)} />
        <div className="grid gap-4 sm:grid-cols-2">
            <label className="text-xs font-medium text-theme-text">Tool call parser<Input className="mt-1.5" value={value.toolParser} required={value.toolCalling} disabled={!value.toolCalling} pattern="[a-z0-9_]*" placeholder="hermes" onChange={event => set('toolParser', event.target.value)} /></label>
            <label className="text-xs font-medium text-theme-text">Reasoning parser<Input className="mt-1.5" value={value.reasoningParser} pattern="[a-z0-9_]*" placeholder="Runtime default" onChange={event => set('reasoningParser', event.target.value)} /></label>
            <label className="text-xs font-medium text-theme-text">Runtime dtype<Input className="mt-1.5" value={value.dtype} placeholder="Runtime default" onChange={event => set('dtype', event.target.value)} /></label>
        </div>
        <label className="block text-xs font-medium text-theme-text">Chat template<Input className="mt-1.5 font-mono" value={value.chatTemplate} placeholder="Runtime default; template text or a path inside the runtime container" onChange={event => set('chatTemplate', event.target.value)} /></label>
        <label className="block text-xs font-medium text-theme-text">Additional vLLM arguments<Textarea className="mt-1.5 font-mono" rows={3} value={value.extra} placeholder={'kv-cache-dtype=fp8'} onChange={event => set('extra', event.target.value)} /></label>
        <p className="text-xs text-theme-muted">One name=value per line. Use the fields above for tool calling, parsers, dtype and chat template. Context, batching, memory and parallelism belong to the deployment plan.</p>
    </div>;
}
