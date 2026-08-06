import type { RunStreamEvent } from '../../api/events';

export interface LiveToolActivity {
  sequence: number;
  toolName: string;
  status: 'running' | 'completed';
}

export interface ChatStreamState {
  reasoning: string;
  assistant: string;
  tools: LiveToolActivity[];
}

export const emptyChatStream: ChatStreamState = {
  reasoning: '',
  assistant: '',
  tools: [],
};

export function applyChatStreamEvent(
  state: ChatStreamState,
  event: RunStreamEvent,
): ChatStreamState {
  if (event.event_type === 'model.stream') {
    const rawType = String(event.payload.raw_type ?? '');
    const delta = event.payload.delta;
    if (typeof delta !== 'string') return state;
    if (
      rawType === 'response.reasoning_text.delta'
      || rawType === 'response.reasoning_summary_text.delta'
    ) {
      return { ...state, reasoning: state.reasoning + delta };
    }
    if (rawType === 'response.output_text.delta') {
      return { ...state, assistant: state.assistant + delta };
    }
    return state;
  }

  if (event.event_type === 'tool.started') {
    const toolName = event.payload.tool_name;
    if (typeof toolName !== 'string') return state;
    return {
      ...state,
      tools: [...state.tools, { sequence: event.sequence, toolName, status: 'running' }],
    };
  }

  if (event.event_type === 'tool.completed') {
    const toolName = event.payload.tool_name;
    if (typeof toolName !== 'string') return state;
    let index = -1;
    for (let toolIndex = state.tools.length - 1; toolIndex >= 0; toolIndex -= 1) {
      const tool = state.tools[toolIndex];
      if (tool.toolName === toolName && tool.status === 'running') {
        index = toolIndex;
        break;
      }
    }
    if (index < 0) {
      return {
        ...state,
        tools: [...state.tools, { sequence: event.sequence, toolName, status: 'completed' }],
      };
    }
    return {
      ...state,
      tools: state.tools.map((tool, toolIndex) =>
        toolIndex === index ? { ...tool, status: 'completed' } : tool,
      ),
    };
  }

  if (event.event_type === 'run.item' && !state.assistant) {
    const item = event.payload.item;
    if (
      typeof item === 'object'
      && item !== null
      && (item as Record<string, unknown>).type === 'message_output_item'
    ) {
      const content = (item as Record<string, unknown>).content;
      if (typeof content === 'string') return { ...state, assistant: content };
    }
  }

  return state;
}
