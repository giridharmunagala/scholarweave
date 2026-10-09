import { describe, expect, it } from 'vitest';
import { groupByRecency } from './ChatPage';

describe('groupByRecency', () => {
  it('buckets chats by local calendar day and preserves order', () => {
    const now = new Date(2025, 8, 20, 15, 0);
    const at = (days: number, hour = 12) =>
      ({ id: `${days}-${hour}`, updated_at: new Date(2025, 8, 20 - days, hour).toISOString() });
    const items = [at(0, 9), at(0, 1), at(1, 23), at(3), at(20), at(90), { id: 'bad', updated_at: 'nope' }];

    const groups = groupByRecency(items, now);

    expect(groups.map((group) => [group.label, group.items.map((item) => item.id)])).toEqual([
      ['Today', ['0-9', '0-1']],
      ['Yesterday', ['1-23']],
      ['Previous 7 days', ['3-12']],
      ['Previous 30 days', ['20-12']],
      ['Older', ['90-12', 'bad']],
    ]);
  });
});
