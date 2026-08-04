import type { WorkflowChoice } from '../components/workflow/WorkflowPicker';

/**
 * Builds the editor URL for a picked workflow. `documentId` is carried through as a
 * query parameter so the editor can prefill the run inputs with the paper it came from.
 */
export function workflowEditorPath(choice: WorkflowChoice, options?: { documentId?: string }): string {
  const params = new URLSearchParams();
  if (options?.documentId) {
    params.set('documentId', options.documentId);
  }

  if (choice.kind === 'saved') {
    const search = params.toString();
    return `/agents/${choice.workflow.id}${search ? `?${search}` : ''}`;
  }

  if (choice.kind === 'template') {
    params.set('template', choice.definition.name);
  }
  const search = params.toString();
  return `/agents/new${search ? `?${search}` : ''}`;
}
