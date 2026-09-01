import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../shared/components/Icons';
import { ModelSelect, type ModelOption } from '../../shared/components/ModelSelect';
import type { BuiltInSpeechStatus } from '../providers/api';
import type { ModelReference } from './api';

export type SpeechMode = 'builtin' | 'provider';

/**
 * The microphone, and nothing else unless there is a real choice to make.
 *
 * Installing models and picking a default belong in Settings, which already owns
 * them, so the composer only surfaces the source switch when both a local model
 * and a provider model are genuinely available.
 */
export function SpeechControl({
  mode,
  onModeChange,
  builtIn,
  options,
  modelReference,
  onModelReferenceChange,
  recording,
  starting,
  transcribing,
  busy,
  onToggle,
}: {
  mode: SpeechMode;
  onModeChange: (mode: SpeechMode) => void;
  builtIn: BuiltInSpeechStatus | null;
  options: ModelOption[];
  modelReference: ModelReference;
  onModelReferenceChange: (reference: ModelReference) => void;
  recording: boolean;
  starting: boolean;
  transcribing: boolean;
  busy: boolean;
  onToggle: () => void;
}) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const localReady = builtIn?.state === 'ready' || builtIn?.state === 'running';
  const providerReady = Boolean(modelReference.provider_profile_id && modelReference.model);
  const ready = mode === 'builtin' ? localReady : providerReady;
  const canChoose = localReady && options.length > 0;

  const hint = ready
    ? recording ? 'Stop and transcribe' : 'Dictate'
    : 'Set up speech recognition in Settings';

  return (
    <div className="speech-control" ref={containerRef}>
      <button
        type="button"
        className={`composer-icon${recording ? ' recording' : ''}`}
        aria-label={recording ? 'Stop recording and transcribe' : 'Dictate'}
        title={hint}
        disabled={busy || starting || transcribing || !ready}
        onClick={onToggle}
      >
        {starting || transcribing
          ? <span className="spinner tiny" aria-hidden="true" />
          : <Icon name={recording ? 'stop' : 'microphone'} size={16} />}
      </button>

      {canChoose ? (
        <button
          type="button"
          className="speech-source-toggle"
          aria-label="Speech source"
          aria-expanded={open}
          aria-haspopup="true"
          title="Speech source"
          disabled={recording || starting || transcribing}
          onClick={() => setOpen((value) => !value)}
        >
          <Icon name="arrowRight" size={10} />
        </button>
      ) : null}

      {open ? (
        <div className="speech-popover" role="dialog" aria-label="Speech source">
          <label className="speech-choice">
            <input
              type="radio"
              name="speech-source"
              checked={mode === 'builtin'}
              onChange={() => onModeChange('builtin')}
            />
            <span>On-device</span>
          </label>
          <label className="speech-choice">
            <input
              type="radio"
              name="speech-source"
              checked={mode === 'provider'}
              onChange={() => onModeChange('provider')}
            />
            <span>Provider model</span>
          </label>
          {mode === 'provider' ? (
            <ModelSelect
              options={options}
              value={modelReference}
              placeholder="Choose a speech model"
              onChange={(reference) => onModelReferenceChange(reference as ModelReference)}
            />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
