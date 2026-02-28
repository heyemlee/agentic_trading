/* A confirmation belongs to both the proposal and the authority the person reviewed. */
window.ApprovalIntent = {
  authorityMatches(proposal, authority) {
    if (!proposal || typeof authority?.version !== 'string' || !authority.version) return false;
    const change = proposal.authority_changed;
    return change == null ? proposal.authority_version === authority.version
      : change.from === (proposal.authority_version ?? null) && change.to === authority.version &&
        ['tightened', 'loosened', 'mixed', 'unrelated', 'unknown'].includes(change.classification);
  },
  needsAcknowledgement(proposal) {
    return ['loosened', 'mixed', 'unknown'].includes(proposal?.authority_changed?.classification);
  },
  capture(proposal, requestId, authority) {
    if (!proposal || !proposal.proposal_id || !proposal.version) throw new Error('A current proposal version is required.');
    if (!this.authorityMatches(proposal, authority)) throw new Error('A matching authority reading is required.');
    return Object.freeze({ proposal_id: proposal.proposal_id, version: proposal.version,
      authority_version: authority.version,
      ticker: proposal.ticker, bucket: proposal.bucket, action: proposal.action,
      notional_usd: proposal.notional_usd, sell_quantity: proposal.sell_quantity ?? null,
      idempotency_key: requestId });
  },
  matches(intent, proposal, authority) {
    return !!(intent && this.authorityMatches(proposal, authority) && intent.authority_version === authority.version &&
      ['proposal_id', 'version', 'ticker', 'bucket', 'action', 'notional_usd']
      .every(key => intent[key] === proposal[key]) && intent.sell_quantity === (proposal.sell_quantity ?? null));
  },
};
