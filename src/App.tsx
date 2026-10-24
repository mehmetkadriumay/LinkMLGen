import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Background, Controls, Handle, MiniMap, Position, ReactFlow,
  type Connection, type NodeProps, type Node, type XYPosition, type ReactFlowInstance,
} from '@xyflow/react';
import {
  changeSlot, classReferences, effectiveSlots, initialYaml, ownSlots, readSchema,
  removeClass, writeSchema, type Schema, type Slot,
} from './schema';

type Message = { role: 'user' | 'assistant'; content: string };
type Proposal = { message: string; questions: string[]; proposed_yaml: string | null; validation_errors: string[]; validation_warnings?: string[] };
type Validation = { valid: boolean; errors: string[]; warnings: string[]; schema: Schema | null; ranges: string[] };
type ClassNode = Node<{ name: string; description: string; slots: Record<string, Slot> }, 'class'>;
type Artifact = { content: Blob; filename: string; warningCount: number };
const primitives = ['string', 'integer', 'float', 'double', 'boolean', 'date', 'datetime', 'uri', 'uriorcurie', 'decimal', 'time'];
const welcome: Message = {
  role: 'assistant',
  content: 'Let\'s build a model together. What kind of data are you working with, and what do you want to do with it? You can start from the example, import YAML, or describe a new domain.',
};

async function api<T>(path: string, body?: unknown, signal?: AbortSignal, decode?: (response: Response) => Promise<T>): Promise<T> {
  const response = await fetch(`/api/${path}`, body === undefined ? { signal } : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail));
  }
  return decode ? decode(response) : response.json() as Promise<T>;
}

async function readArtifact(response: Response): Promise<Artifact> {
  const filename = response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1];
  const count = response.headers.get('X-Export-Warning-Count');
  if (!filename || count === null || !/^\d+$/.test(count)) throw new Error('The export response is missing download metadata.');
  return { content: await response.blob(), filename, warningCount: Number(count) };
}

function download(name: string, content: string | Blob, type: string) {
  const url = URL.createObjectURL(content instanceof Blob ? content : new Blob([content], { type }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function ClassCard({ data, selected }: NodeProps<ClassNode>) {
  return <div className={`class-card ${selected ? 'selected' : ''}`}>
    <Handle type="target" position={Position.Left} />
    <div className="class-heading"><span className="class-icon">C</span><strong>{data.name}</strong><small>CLASS</small></div>
    {data.description && <p>{data.description}</p>}
    <div className="class-slots">{Object.entries(data.slots).map(([name, slot]) =>
      <div className="slot-row" key={name}><span>{slot.identifier ? '# ' : ''}{name}{slot.required ? ' *' : ''}</span>
        <code>{slot.range ?? 'string'}{slot.multivalued ? '[]' : ''}</code></div>)}
      {!Object.keys(data.slots).length && <small>No properties yet</small>}
    </div>
    <Handle type="source" position={Position.Right} />
  </div>;
}
const nodeTypes = { class: ClassCard };

function OperationBanner({ label, onCancel }: { label: string; onCancel: () => void }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const started = Date.now();
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, []);
  return <div className="operation-banner" role="status">
    <div><strong>{label} · {elapsed}s</strong>
      <small>Large schemas can take a few minutes. Your YAML is preserved while you wait.</small></div>
    <button onClick={onCancel}>Cancel operation</button>
  </div>;
}

function isCanceled(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

export default function App() {
  const [yaml, setYaml] = useState(initialYaml);
  const [draft, setDraft] = useState(initialYaml);
  const [schema, setSchema] = useState<Schema>(() => readSchema(initialYaml));
  const [selected, setSelected] = useState('Person');
  const [tab, setTab] = useState<'visual' | 'yaml'>('visual');
  const [positions, setPositions] = useState<Record<string, XYPosition>>({});
  const [history, setHistory] = useState([initialYaml]);
  const [historyIndex, setHistoryIndex] = useState(0);
  const [messages, setMessages] = useState<Message[]>([welcome]);
  const [input, setInput] = useState('');
  const [proposal, setProposal] = useState<(Proposal & { base: string }) | null>(null);
  const [busy, setBusy] = useState(true);
  const [operation, setOperation] = useState('Connecting to LinkML service');
  const pending = useRef<AbortController | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [availableRanges, setAvailableRanges] = useState<string[]>([]);
  const [status, setStatus] = useState('Connecting to LinkML service...');
  const [aiReady, setAiReady] = useState(false);
  const [aiConfigurationError, setAiConfigurationError] = useState('Configure Microsoft Foundry in backend\\.env and restart the backend to enable chat.');
  const [className, setClassName] = useState('');
  const [slotName, setSlotName] = useState('');
  const [slotRange, setSlotRange] = useState('string');
  const [enumName, setEnumName] = useState('');
  const [enumValues, setEnumValues] = useState('');
  const [exportScope, setExportScope] = useState('selected');
  const [exportAuthority, setExportAuthority] = useState('osdu');
  const [exportSource, setExportSource] = useState('wks');
  const [exportVersion, setExportVersion] = useState('');
  const [exportGroup, setExportGroup] = useState('master-data');
  const [fabricSchema, setFabricSchema] = useState('dbo');
  const currentYaml = useRef(yaml);
  const flow = useRef<ReactFlowInstance<ClassNode> | null>(null);
  currentYaml.current = yaml;
  const dirty = draft !== yaml;
  const locked = busy || dirty;

  async function request<T>(path: string, body: unknown, label: string, decode?: (response: Response) => Promise<T>): Promise<T> {
    if (pending.current && !pending.current.signal.aborted) throw new Error('Another operation is already in progress.');
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true); setOperation(label);
    const timeout = setTimeout(() => controller.abort(new DOMException(
      'The request timed out after 5 minutes. Your YAML is preserved; check the backend and retry.', 'TimeoutError',
    )), 300_000);
    try {
      const result = await api<T>(path, body, controller.signal, decode);
      controller.signal.throwIfAborted();
      return result;
    } catch (error) {
      if (controller.signal.aborted) throw controller.signal.reason;
      throw error;
    } finally {
      clearTimeout(timeout);
      if (pending.current === controller) { pending.current = null; setBusy(false); }
    }
  }

  function cancelOperation() {
    pending.current?.abort();
    pending.current = null;
    setBusy(false);
    setStatus('Operation canceled · unapplied YAML is preserved');
  }

  function reportFailure(error: unknown) {
    if (isCanceled(error)) return;
    setErrors([String(error)]); setStatus('Request failed · YAML is preserved');
  }

  async function exportArtifact(format: 'schemas' | 'manifests' | 'postgresql' | 'fabric') {
    setErrors([]);
    try {
      const relational = format === 'postgresql' || format === 'fabric';
      if (!relational && exportScope === 'selected' && !selected) throw new Error('Select a class to export.');
      const result = await request<Artifact>(
        relational ? `export/${format}` : `export/osdu/${format}`,
        format === 'fabric' ? { yaml, target_schema: fabricSchema } : format === 'postgresql' ? { yaml } : {
          yaml, class_names: exportScope === 'selected' ? [selected] : [],
          authority: exportAuthority, source: exportSource, version: exportVersion || null, group_type: exportGroup,
        },
        format === 'fabric' ? 'Generating Fabric Lakehouse notebook and Spark SQL' :
          format === 'postgresql' ? 'Generating PostgreSQL DDL' : `Generating OSDU ${format}`,
        readArtifact,
      );
      download(result.filename, result.content, result.content.type);
      setStatus(result.warningCount ? `Export downloaded · ${result.warningCount} notices; review the export report or SQL comments` : 'Export downloaded');
    } catch (error) { reportFailure(error); }
  }

  useEffect(() => {
    let active = true;
    async function initialize() {
      try {
        const health = await request<{ ai_configured: boolean; ai_configuration_error?: string | null }>('health', undefined, 'Connecting to LinkML service');
        if (!active) return;
        setAiReady(health.ai_configured);
        if (health.ai_configuration_error) setAiConfigurationError(health.ai_configuration_error);
        const saved = localStorage.getItem('linkml-studio-schema');
        const text = saved ?? initialYaml;
        setDraft(text);
        if (saved) setTab('yaml');
        const result = await request<Validation>('validate', { yaml: text }, 'Validating saved schema');
        if (!active) return;
        setDraft(text);
        setWarnings(result.warnings);
        if (result.valid && result.schema) {
          setYaml(text); setSchema(result.schema); setHistory([text]); setHistoryIndex(0);
          setAvailableRanges(result.ranges);
          setSelected(Object.keys(result.schema.classes ?? {})[0] ?? '');
          setStatus(result.warnings.length ? `Loaded with ${result.warnings.length} validation warnings` :
            saved ? 'Restored local draft · LinkML valid' : 'LinkML valid');
          setTab('visual');
        } else {
          setErrors(result.errors); setTab('yaml'); setStatus('Saved draft needs attention');
        }
      } catch (error) {
        if (active) reportFailure(error);
      }
    }
    void initialize();
    return () => { active = false; pending.current?.abort(); };
  }, []);

  function persist(text: string) {
    try { localStorage.setItem('linkml-studio-schema', text); }
    catch { setErrors(previous => [...previous, 'Browser storage is unavailable. Download YAML to save your work.']); }
  }

  async function commit(text: string, index?: number) {
    if (busy) return;
    setErrors([]);
    try {
      const result = await request<Validation>('validate', { yaml: text }, 'Validating and applying schema');
      setWarnings(result.warnings);
      if (!result.valid || !result.schema) { setErrors(result.errors); setStatus('Not applied · fix validation errors'); return; }
      setYaml(text); setDraft(text); setSchema(result.schema); setProposal(null);
      setAvailableRanges(result.ranges);
      if (index !== undefined) setHistoryIndex(index);
      else {
        const next = [...history.slice(0, historyIndex + 1), text];
        setHistory(next); setHistoryIndex(next.length - 1);
      }
      if (!result.schema.classes?.[selected]) setSelected(Object.keys(result.schema.classes ?? {})[0] ?? '');
      setStatus(result.warnings.length ? `Loaded with ${result.warnings.length} validation warnings` : 'LinkML valid · saved locally'); persist(text);
    } catch (error) { reportFailure(error); }
  }

  function edit(next: Schema) {
    if (locked) return;
    void commit(writeSchema(next));
  }

  const nodes: ClassNode[] = useMemo(() => Object.entries(schema.classes ?? {}).map(([name, cls], index) => ({
    id: name, type: 'class', selected: name === selected,
    position: positions[name] ?? { x: (index % 2) * 360 + 50, y: Math.floor(index / 2) * 290 + 60 },
    data: { name, description: cls.description ?? '', slots: effectiveSlots(schema, name) },
  })), [schema, selected, positions]);
  const edges = useMemo(() => Object.entries(schema.classes ?? {}).flatMap(([name, cls]) => [
    ...[cls.is_a, ...(cls.mixins ?? [])].filter((parent): parent is string => Boolean(parent))
      .map(parent => ({ id: `${name}:parent:${parent}`, source: name, target: parent,
        label: cls.is_a === parent ? 'is a' : 'mixin', style: { strokeDasharray: '6 4', stroke: '#8b7ac7' } })),
    ...Object.entries(ownSlots(schema, name)).filter(([, slot]) => Boolean(schema.classes?.[slot.range ?? '']))
      .map(([slot, definition]) => ({
        id: `${name}:slot:${slot}`, source: name, target: definition.range!,
        label: `${slot} ${definition.required ? '1' : '0'}..${definition.multivalued ? '*' : '1'}`,
        style: { stroke: '#16a398', strokeWidth: 2 },
      })),
  ]), [schema]);

  function focusClass(name: string) {
    setSelected(name);
    const node = nodes.find(candidate => candidate.id === name);
    if (node) void flow.current?.setCenter(node.position.x + 127, node.position.y + 110, { zoom: 1, duration: 250 });
  }

  function connect(connection: Connection) {
    if (locked || !connection.source || !connection.target) return;
    const next = structuredClone(schema);
    const cls = next.classes![connection.source];
    const base = connection.target.replace(/([a-z])([A-Z])/g, '$1_$2').toLowerCase();
    let name = base; let suffix = 2;
    const existing = effectiveSlots(schema, connection.source);
    while (existing[name]) name = `${base}_${suffix++}`;
    cls.attributes = { ...cls.attributes, [name]: { range: connection.target } };
    edit(next);
  }

  async function send(text: string) {
    if (locked || !text.trim()) return;
    if (messages.length >= 39) { setErrors(['Conversation limit reached. Start a new conversation to continue.']); return; }
    const next: Message[] = [...messages, { role: 'user', content: text.trim() }];
    setMessages(next); setInput(''); setProposal(null); setErrors([]);
    const base = yaml;
    try {
      const result = await request<Proposal>('chat', { yaml: base, messages: next.slice(1) }, 'Preparing AI guidance');
      const questions = result.questions.map((question, index) => `${index + 1}. ${question}`).join('\n');
      setMessages([...next, { role: 'assistant', content: [result.message, questions].filter(Boolean).join('\n\n') }]);
      setProposal({ ...result, base });
    } catch (error) {
      reportFailure(error);
      if (!isCanceled(error)) { setMessages(next.slice(0, -1)); setInput(text); }
    }
  }

  const cls = schema.classes?.[selected];
  const slots = cls ? effectiveSlots(schema, selected) : {};
  const ranges = [...new Set([...primitives, ...Object.keys(schema.classes ?? {}), ...Object.keys(schema.enums ?? {}), ...Object.keys(schema.types ?? {}), ...availableRanges])];

  return <div className="app">
    <header className="topbar">
      <div className="brand"><span className="logo">L<span>m</span></span><div>LinkML <strong>Studio</strong> <span className="osdu-ready">OSDU Ready</span><small>FROM IDEAS TO CONNECTED DATA</small></div></div>
      <div className="project-name">{schema.name}<span className="badge">LOCAL WORKSPACE</span></div>
      <div className="toolbar">
        <button disabled={locked || historyIndex === 0} onClick={() => void commit(history[historyIndex - 1], historyIndex - 1)}>Undo</button>
        <button disabled={locked || historyIndex === history.length - 1} onClick={() => void commit(history[historyIndex + 1], historyIndex + 1)}>Redo</button>
        <label className={`button ${busy ? 'disabled' : ''}`}>Import YAML<input type="file" accept=".yaml,.yml" disabled={busy}
          onChange={async event => {
            const file = event.target.files?.[0]; event.target.value = '';
            if (!file) return;
            if (dirty) { setErrors(['Apply or discard your YAML edits before importing.']); return; }
            try { const text = await file.text(); setDraft(text); setTab('yaml'); await commit(text); }
            catch (error) { setErrors([String(error)]); }
          }} /></label>
        <button className="primary" disabled={dirty || busy} onClick={() => download(`${schema.name}.yaml`, yaml, 'text/yaml')}>Export YAML</button>
      </div>
    </header>
    <main className="workspace">
      <aside className="assistant panel">
        <div className="panel-title"><div><span className="eyebrow">YOUR MODELING PARTNER</span><h2>Build through conversation</h2></div><span className="spark">AI</span></div>
        <div className="steps"><span className="active">Discover</span><span>Model</span><span>Refine</span></div>
        <p className="privacy">{aiReady ? 'Microsoft Foundry configured. Sending a message shares the schema, its registered local imports, and conversation with your configured Foundry endpoint.' :
          `Visual editing works without AI. ${aiConfigurationError}`}</p>
        <div className="conversation" aria-live="polite">{messages.map((message, index) =>
          <div className={`message ${message.role}`} key={index}><small>{message.role === 'assistant' ? 'LINKML GUIDE' : 'YOU'}</small><p>{message.content}</p></div>)}
          {busy && <p className="muted" role="status">Working...</p>}
          {proposal?.proposed_yaml && <div className="proposal">
            {proposal.proposed_yaml && <><strong>Suggested schema update</strong>
              <details><summary>Review current and proposed YAML</summary><small>CURRENT</small><pre>{proposal.base}</pre><small>PROPOSED</small><pre>{proposal.proposed_yaml}</pre></details>
              {proposal.validation_errors.length > 0 && <div className="error">Proposal is invalid: {proposal.validation_errors.join('; ')}</div>}
              {(proposal.validation_warnings?.length ?? 0) > 0 && <details className="warnings"><summary>Proposal validation warnings</summary>
                {proposal.validation_warnings!.map((warning, index) => <p key={index}>{warning}</p>)}</details>}
              <button className="primary" disabled={locked || proposal.validation_errors.length > 0} onClick={() => {
                if (currentYaml.current !== proposal.base) { setErrors(['The schema has changed. Ask for a fresh proposal.']); return; }
                void commit(proposal.proposed_yaml!);
              }}>Apply proposed changes</button> <button disabled={busy} onClick={() => setProposal(null)}>Dismiss</button></>}
          </div>}
        </div>
        <form className="chat-form" onSubmit={event => { event.preventDefault(); void send(input); }}>
          <textarea aria-label="Message to the modeling assistant" placeholder="Describe your data or answer a question..." value={input} maxLength={12_000}
            onChange={event => setInput(event.target.value)} disabled={busy} />
          <div><button type="button" disabled={busy} onClick={() => { setMessages([welcome]); setProposal(null); }}>New chat</button>
            <button className="primary" disabled={locked || !aiReady || !input.trim()}>Send message</button></div>
        </form>
      </aside>
      <section className="canvas-panel">
        <div className="canvas-toolbar">
          <div className="tabs"><button className={tab === 'visual' ? 'active' : ''} onClick={() => setTab('visual')}>Visual builder</button>
            <button className={tab === 'yaml' ? 'active' : ''} onClick={() => setTab('yaml')}>YAML {dirty ? '*' : ''}</button></div>
          <span className="muted">{Object.keys(schema.classes ?? {}).length} classes · {Object.keys(schema.enums ?? {}).length} enums</span>
        </div>
        {busy && <OperationBanner key={operation} label={operation} onCancel={cancelOperation} />}
        {dirty && <div className="notice">{busy ? 'Your YAML is being processed. Wait, cancel the operation, or discard the unapplied edits.' :
          'YAML has unapplied changes. Apply or discard them to resume visual editing and chat.'}</div>}
        {tab === 'visual' ? <div className="diagram">
          <div className="canvas-hint">Your model, connected.<small>Select a class to edit. Drag between handles to add a relationship.</small></div>
          <ReactFlow<ClassNode> nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView onlyRenderVisibleElements
            onInit={instance => { flow.current = instance; }}
            nodesConnectable={!locked} nodesDraggable={!busy} deleteKeyCode={null}
            onNodeClick={(_, node) => { setSelected(node.id); setSlotName(''); }}
            onNodesChange={changes => setPositions(previous => {
              const moved = changes.filter(change => change.type === 'position' && change.position &&
                (previous[change.id]?.x !== change.position.x || previous[change.id]?.y !== change.position.y));
              if (!moved.length) return previous;
              const next = { ...previous };
              for (const change of moved) if (change.type === 'position' && change.position) next[change.id] = change.position;
              return next;
            })}
            onConnect={connect} minZoom={0.2} maxZoom={1.6}>
            <Background color="#ccd5e1" gap={22} /><Controls /><MiniMap pannable zoomable nodeColor="#d3eee9" />
          </ReactFlow>
        </div> : <div className="yaml-pane">
          <div className="yaml-actions"><span>LinkML YAML · full document</span><div>
            <button disabled={!dirty} onClick={() => {
              cancelOperation(); setDraft(yaml); persist(yaml); setErrors([]); setStatus('Unapplied edits discarded');
            }}>Discard edits</button>
            <button className="primary" disabled={busy} onClick={() => void commit(draft)}>{busy ? 'Processing...' : 'Validate & apply'}</button></div></div>
          <textarea spellCheck={false} aria-label="LinkML YAML editor" value={draft} disabled={busy}
            onChange={event => { setDraft(event.target.value); persist(event.target.value); }} />
        </div>}
        <div className="diagnostics" aria-live="polite">
          <span className={`status-dot ${errors.length ? 'invalid' : ''}`} /><span>{status}</span>
          <button disabled={busy} onClick={async () => {
            setErrors([]);
            try {
              const result = await request<Validation>('validate', { yaml: draft }, 'Validating schema');
              setErrors(result.errors); setWarnings(result.warnings);
              setStatus(!result.valid ? 'Validation errors' : result.warnings.length ?
                `${result.warnings.length} validation warnings${dirty ? ' · draft not applied' : ''}` :
                dirty ? 'Draft is valid · not yet applied' : 'LinkML valid');
            } catch (error) { reportFailure(error); }
          }}>Validate</button>
        </div>
        {warnings.length > 0 && <details className="warnings"><summary>{warnings.length} validation warnings · schema retained unchanged</summary>
          {warnings.map((warning, index) => <p key={index}>{warning}</p>)}</details>}
        {errors.length > 0 && <div className="errors" role="alert">{errors.map((error, index) => <p key={index}>{error}</p>)}<button onClick={() => setErrors([])}>Dismiss</button></div>}
      </section>
      <aside className="inspector panel">
        <div className="panel-title"><div><span className="eyebrow">MODEL DETAILS</span><h2>Property inspector</h2></div></div>
        <fieldset disabled={locked}>
          <details className="inspector-section"><summary>Schema settings</summary>
            <label>Schema name<input key={`name-${schema.name}`} defaultValue={schema.name} onBlur={event => {
              if (event.target.value !== schema.name) edit({ ...schema, name: event.target.value });
            }} /></label>
            <label>Schema URI<input key={`id-${schema.id}`} defaultValue={schema.id} onBlur={event => {
              if (event.target.value !== schema.id) edit({ ...schema, id: event.target.value });
            }} /></label>
            <p className="muted">Edit prefixes, global slots, inheritance, and advanced constraints in YAML. Existing metadata is preserved.</p>
          </details>
          <form className="add-class" onSubmit={event => {
            event.preventDefault();
            const name = className.trim();
            if (!name || schema.classes?.[name]) { setErrors(['Enter a unique class name.']); return; }
            edit({ ...schema, classes: { ...schema.classes, [name]: { description: '', attributes: {} } } });
            setSelected(name); setClassName('');
          }}><input aria-label="New class name" placeholder="New class name" value={className} pattern="[A-Za-z_][A-Za-z0-9_]*" onChange={event => setClassName(event.target.value)} required />
            <button className="primary">+ Class</button></form>
          <label>Selected class<select value={selected} onChange={event => focusClass(event.target.value)}>
            <option value="">Choose a class</option>{Object.keys(schema.classes ?? {}).map(name => <option key={name}>{name}</option>)}
          </select></label>
          {cls && <div key={selected}>
            <h3>{selected}<span className="badge">CLASS</span></h3>
            <label>Description<textarea key={`${selected}-${cls.description}`} defaultValue={cls.description ?? ''} onBlur={event => {
              if (event.target.value !== (cls.description ?? '')) edit({ ...schema, classes: { ...schema.classes, [selected]: { ...cls, description: event.target.value } } });
            }} /></label>
            <h4>Properties <span>{Object.keys(slots).length}</span></h4>
            {Object.entries(slots).map(([name, slot]) => <details className="slot-editor" key={`${selected}-${name}`}>
              <summary>{slot.identifier ? '# ' : ''}{name} <code>{slot.range ?? schema.default_range ?? 'string'}</code></summary>
              <label>Range<select value={slot.range ?? schema.default_range ?? 'string'} onChange={event => edit(changeSlot(schema, selected, name, { range: event.target.value }))}>
                {[...new Set([...ranges, slot.range ?? schema.default_range ?? 'string'])].map(range => <option key={range}>{range}</option>)}
              </select></label>
              <label>Description<textarea key={slot.description ?? ''} defaultValue={slot.description ?? ''} onBlur={event => {
                if (event.target.value !== (slot.description ?? '')) edit(changeSlot(schema, selected, name, { description: event.target.value }));
              }} /></label>
              {(['required', 'multivalued', 'identifier'] as const).map(flag => <label className="checkbox" key={flag}>
                <input type="checkbox" checked={Boolean(slot[flag])} onChange={event => edit(changeSlot(schema, selected, name, { [flag]: event.target.checked }))} />
                {flag === 'multivalued' ? 'Multiple values' : flag[0].toUpperCase() + flag.slice(1)}</label>)}
              {cls.attributes?.[name] ? <button className="danger" onClick={() => {
                const next = structuredClone(schema); delete next.classes![selected].attributes![name]; edit(next);
              }}>Remove property</button> : <small>Shared or inherited property. Edits apply to this class only; use YAML to detach.</small>}
            </details>)}
            <form className="new-slot" onSubmit={event => {
              event.preventDefault(); const name = slotName.trim();
              if (!name || slots[name]) { setErrors(['Enter a unique property name.']); return; }
              edit({ ...schema, classes: { ...schema.classes, [selected]: { ...cls, attributes: { ...cls.attributes, [name]: { range: slotRange } } } } });
              setSlotName('');
            }}><label>New property<input value={slotName} pattern="[A-Za-z_][A-Za-z0-9_]*" required placeholder="e.g. email" onChange={event => setSlotName(event.target.value)} /></label>
              <label>Range<select value={slotRange} onChange={event => setSlotRange(event.target.value)}>{ranges.map(range => <option key={range}>{range}</option>)}</select></label>
              <button>+ Add property</button></form>
            <button className="danger" onClick={() => {
              const references = classReferences(schema, selected);
              if (references.length) { setErrors([`Cannot delete ${selected}: referenced by ${references.join(', ')}. Remove references first.`]); return; }
              edit(removeClass(schema, selected));
            }}>Delete class</button>
          </div>}
          <details className="inspector-section"><summary>Controlled vocabularies</summary>
            {Object.keys(schema.enums ?? {}).map(name => <p key={name}><code>{name}</code></p>)}
            <form onSubmit={event => {
              event.preventDefault(); const name = enumName.trim();
              const values = [...new Set(enumValues.split('\n').map(value => value.trim()).filter(Boolean))];
              if (!name || schema.enums?.[name] || !values.length) { setErrors(['Enter a unique enum name and at least one value.']); return; }
              edit({ ...schema, enums: { ...schema.enums, [name]: { permissible_values: Object.fromEntries(values.map(value => [value, {}])) } } });
              setEnumName(''); setEnumValues('');
            }}><label>Enum name<input required pattern="[A-Za-z_][A-Za-z0-9_]*" value={enumName} onChange={event => setEnumName(event.target.value)} /></label>
              <label>One value per line<textarea required value={enumValues} onChange={event => setEnumValues(event.target.value)} /></label>
              <button>+ Add enum</button></form>
          </details>
        </fieldset>
        <button className="export-json" disabled={locked} onClick={async () => {
          setErrors([]);
          try { const result = await request<{ json_schema: string }>('export/json-schema', { yaml }, 'Generating JSON Schema'); download(`${schema.name}.schema.json`, result.json_schema, 'application/json'); }
          catch (error) { reportFailure(error); }
        }}>Export JSON Schema</button>
        <fieldset className="osdu-exports" disabled={locked}>
          <h3>OSDU exports</h3>
          <label>OSDU export scope<select aria-label="OSDU export scope" value={exportScope} onChange={event => setExportScope(event.target.value)}>
            <option value="selected">Selected class</option><option value="all">All OSDU record classes</option>
          </select></label>
          <label>Schema authority<input value={exportAuthority} onChange={event => setExportAuthority(event.target.value)} /></label>
          <label>Schema source<input value={exportSource} onChange={event => setExportSource(event.target.value)} /></label>
          <label>Export version override<input placeholder="From OSDU metadata, e.g. 1.4.0" value={exportVersion} onChange={event => setExportVersion(event.target.value)} /></label>
          <label>Group for unqualified classes<select value={exportGroup} onChange={event => setExportGroup(event.target.value)}>
            {['master-data', 'reference-data', 'work-product', 'work-product-component', 'dataset'].map(group => <option key={group}>{group}</option>)}
          </select></label>
          <p className="muted">Existing group-qualified classes keep their group. Leave version blank to use source metadata; enter a version for new classes. Registrations use INTERNAL / DEVELOPMENT.</p>
          <button className="export-json" onClick={() => void exportArtifact('schemas')}>Export OSDU schemas + registrations</button>
          <button className="export-json" onClick={() => void exportArtifact('manifests')}>Export OSDU manifest templates</button>
          <p className="muted">ZIP downloads include an export report. Manifest examples contain placeholders and must be completed before ingestion.</p>
          <button className="export-json" onClick={() => void exportArtifact('postgresql')}>Export PostgreSQL DDL (whole model)</button>
        </fieldset>
        <fieldset className="lakehouse-exports" disabled={locked}>
          <h3>Microsoft Fabric Lakehouse</h3>
          <label>Fabric Lakehouse schema<input value={fabricSchema} onChange={event => setFabricSchema(event.target.value)} /></label>
          <p className="muted">Whole-model relational Delta tables. ZIP includes a PySpark notebook, Spark SQL DDL, and name/relationship mappings. Attach a default schema-enabled Lakehouse and create this schema before running.</p>
          <button className="export-json" onClick={() => void exportArtifact('fabric')}>Export Fabric Lakehouse (whole model)</button>
          <p className="muted">Existing tables are never overwritten. Keys and domain rules are metadata, not enforced constraints. Use Spark, not the read-only SQL analytics endpoint.</p>
        </fieldset>
        <p className="inspector-note">Powered by the official LinkML metamodel and generators. Solid edges are relationships; dashed edges are inheritance.</p>
      </aside>
    </main>
  </div>;
}
