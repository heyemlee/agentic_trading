/* A confirmation belongs to one immutable proposal version, never the mutable queue. */
window.ApprovalIntent = {
  capture(proposal, requestId) {
    if (!proposal || !proposal.proposal_id || !proposal.version) throw new Error('A current proposal version is required.');
    return Object.freeze({ proposal_id: proposal.proposal_id, version: proposal.version,
      ticker: proposal.ticker, bucket: proposal.bucket, action: proposal.action,
      notional_usd: proposal.notional_usd, sell_quantity: proposal.sell_quantity ?? null,
      idempotency_key: requestId });
  },
  matches(intent, proposal) {
    return !!(intent && proposal && ['proposal_id', 'version', 'ticker', 'bucket', 'action', 'notional_usd']
      .every(key => intent[key] === proposal[key]) && intent.sell_quantity === (proposal.sell_quantity ?? null));
  },
};
