import { useState } from 'react';
import { useConfirm } from '../common/ConfirmDialog';
import { EmptyState } from '../common/EmptyState';
import { Icon } from '../common/Icon';
import { Modal } from '../common/Modal';
import type { CustomNodeResponse } from '../../types/api';

interface Props {
  definitions: CustomNodeResponse[];
  onClose: () => void;
  onCreate: () => void;
  onEdit: (definition: CustomNodeResponse) => void;
  onDuplicate: (definition: CustomNodeResponse) => void;
  onArchive: (definition: CustomNodeResponse) => Promise<void>;
}

export function CustomNodeLibraryModal({ definitions, onClose, onCreate, onEdit, onDuplicate, onArchive }: Props) {
  const confirm = useConfirm();
  const [archiving, setArchiving] = useState('');

  const archive = async (definition: CustomNodeResponse) => {
    const approved = await confirm({
      title: `Archive “${definition.name}”?`,
      description: 'It will disappear from the palette. Existing workflow nodes stay pinned to their saved revision.',
      confirmLabel: 'Archive node',
    });
    if (!approved) return;
    setArchiving(definition.id);
    try {
      await onArchive(definition);
    } finally {
      setArchiving('');
    }
  };

  return (
    <Modal title="Custom node library" description="Manage reusable, revision-pinned Python transforms." onClose={onClose} className="custom-node-library-modal">
      <div className="custom-library-actions">
        <button type="button" className="button primary" onClick={onCreate}>
          <Icon name="plus" size={13} />
          Create custom node
        </button>
      </div>
      <div className="custom-library-list">
        {definitions.map((definition) => (
          <article className={`custom-library-item${definition.archived ? ' archived' : ''}`} key={definition.id}>
            <div className="custom-library-item-main">
              <div className="custom-library-title">
                <strong>{definition.latest_revision.label}</strong>
                <span className="tiny-tag">r{definition.latest_revision.revision}</span>
                {definition.archived ? <span className="tiny-tag danger">Archived</span> : null}
              </div>
              <code>{definition.name}</code>
              <p>{definition.latest_revision.description || 'No description'}</p>
              <div className="custom-revision-history" aria-label={`Revision history for ${definition.name}`}>
                {definition.revisions.slice().reverse().map((revision) => (
                  <span key={revision.id} title={revision.node_type}>r{revision.revision}</span>
                ))}
              </div>
            </div>
            <div className="button-row compact custom-library-buttons">
              {!definition.archived ? (
                <>
                  <button type="button" className="button subtle sm" onClick={() => onEdit(definition)}>
                    Edit as new revision
                  </button>
                  <button type="button" className="button subtle icon-only sm" aria-label={`Duplicate ${definition.name}`} onClick={() => onDuplicate(definition)}>
                    <Icon name="copy" size={13} />
                  </button>
                  <button type="button" className="button subtle icon-only sm danger-text" aria-label={`Archive ${definition.name}`} disabled={archiving === definition.id} onClick={() => void archive(definition)}>
                    <Icon name="trash" size={13} />
                  </button>
                </>
              ) : null}
            </div>
          </article>
        ))}
        {!definitions.length ? (
          <EmptyState icon="braces" title="No custom nodes yet" description="Create a typed transform to reuse it in any workflow." />
        ) : null}
      </div>
    </Modal>
  );
}
