(function () {
  const labels = {transfer_in: '转账/转赠转入', transfer_out: '转账/转赠转出', balance_recharge: '充值增加', balance_grant: '赠送增加', balance_consume: '消费变动', balance_refund: '退款变动', balance_adjust: '调整净额', points_earn: '积分增加', points_redeem: '积分兑换', points_adjust: '积分调整净额'};
  function add(parent, tag, value) {
    const node = document.createElement(tag);
    node.textContent = value;
    parent.appendChild(node);
    return node;
  }
  window.renderMemberWatch = function (containerId, payload) {
    const box = document.getElementById(containerId);
    if (!box) return;
    box.replaceChildren();
    add(box, 'h3', '会员追踪与核查线索');
    const data = payload && payload.member_watch;
    if (!data) { add(box, 'p', '会员追踪数据暂不可用，请重新读取。'); return; }
    add(box, 'p', `${payload.mode_label || '监控数据'} · ${payload.period.start} 至 ${payload.period.end} · ${data.member_count} 个会员分组 · ${data.flagged_members} 个待核查分组`);
    const notes = add(box, 'details', '');
    add(notes, 'summary', '查看核查规则与币值说明');
    add(notes, 'p', data.coin_value_note);
    add(notes, 'p', data.rule_note);
    add(notes, 'p', '遵循当前日期、门店和资产筛选；数据可能未完整采集。未触发规则不表示不存在异常。币、积分分别展示，不推断兑换关系。');
    if (data.excluded_anonymous_events) add(box, 'p', `${data.excluded_anonymous_events} 条缺少会员标识的记录未参与会员分组。`);
    if (data.truncated) add(box, 'p', '展示前100个分组，优先按线索数和流水数排序；可在后台缩小门店、日期和资产范围。');
    if (!data.members.length) add(box, 'p', '当前范围没有可追踪会员。');
    const venues = new Map();
    for (const member of data.members) {
      if (!venues.has(member.venue)) venues.set(member.venue, []);
      venues.get(member.venue).push(member);
    }
    const stores = add(box, 'div', '');
    stores.className = 'member-watch-stores';
    stores.tabIndex = 0;
    stores.setAttribute('role', 'region');
    stores.setAttribute('aria-label', '按门店展开会员列表');
    for (const [venue, members] of venues) {
      const store = add(stores, 'details', '');
      store.className = 'member-watch-store';
      const flagged = members.filter(member => member.clues.length).length;
      add(store, 'summary', `${venue} · ${data.truncated ? '当前展示' : ''}${members.length} 个会员分组 · ${flagged} 个待核查`);
      for (const member of members) {
        const card = add(store, 'details', '');
        add(card, 'summary', `会员 ${member.member_ref} · ${member.clues.length ? member.clues.length + '项待核查' : '未触发规则'} · ${member.event_count}笔`);
        add(card, 'p', `来源：${member.source}；会员币值：缺少实付数据`);
        for (const clue of member.clues) add(card, 'p', `${clue.asset_name}：${clue.label}，${clue.count}笔${clue.amount == null ? '' : `，涉及数量 ${clue.amount} ${clue.unit}`}；${clue.start} 至 ${clue.end}`);
        for (const asset of member.assets) {
          add(card, 'h4', `${asset.asset_name}（单位：${asset.unit}）`);
          add(card, 'p', Object.entries(asset.changes).map(([kind, amount]) => `${labels[kind] || kind}：${amount.toLocaleString('zh-CN', {maximumFractionDigits: 2})}`).join('；'));
        }
      }
    }
  };
})();
