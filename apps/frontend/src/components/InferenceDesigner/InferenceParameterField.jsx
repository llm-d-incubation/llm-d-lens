/** A source-owned parameter with its purpose and an optional exact-path disclosure. */
export function InferenceParameterField({ field, children }) {
    return <div className="inference-owned-field">
        <div><strong>{field.title}</strong><small>{field.section}</small><p>{field.hint}</p></div>
        <div>{children}</div>
        <details><summary>Source field</summary><code>{field.sourceKey || field.key}</code></details>
    </div>;
}
