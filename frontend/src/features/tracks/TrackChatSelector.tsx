import { useEffect, useRef, useState } from 'react';
import { Link } from '../../app/router';
import { tracksApi, type Track } from './api';
import './tracks.css';

export function TrackChatSelector({ conversationId, trackId, disabled, onChange, onError }: {
  conversationId?: string;
  trackId: string;
  disabled: boolean;
  onChange: (id: string) => void;
  onError: (reason: unknown) => void;
}) {
  const [tracks, setTracks] = useState<Track[]>([]);
  const [saving, setSaving] = useState(false);
  const requestId = useRef(0);
  useEffect(() => {
    let active = true;
    requestId.current += 1;
    setSaving(false);
    tracksApi.list().then((items) => { if (active) setTracks(items); }).catch((reason) => { if (active) onError(reason); });
    return () => { active = false; requestId.current += 1; };
  }, [conversationId]);
  const change = async (id: string) => {
    if (saving || disabled || id === trackId) return;
    const request = requestId.current;
    setSaving(true);
    try {
      if (conversationId) {
        if (id) await tracksApi.conversation(id, conversationId, true);
        else if (trackId) await tracksApi.conversation(trackId, conversationId, false);
      }
      if (request === requestId.current) onChange(id);
    } catch (reason) { if (request === requestId.current) onError(reason); } finally { if (request === requestId.current) setSaving(false); }
  };
  return <div className="track-association">
    <label>Track <select aria-label="Conversation research track" value={trackId} disabled={disabled || saving} onChange={(event) => void change(event.target.value)}>
      <option value="">No research track</option>
      {trackId && !tracks.some((item) => item.id === trackId) ? <option value={trackId}>Linked track {trackId}</option> : null}
      {tracks.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
    </select></label>
    {trackId ? <Link to={`/tracks?id=${encodeURIComponent(trackId)}`}>Open track</Link> : null}
  </div>;
}
