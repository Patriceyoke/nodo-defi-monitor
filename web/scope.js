/* Scope of the dashboard: Io, Moglie, Combinato, or one wallet by name. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.NodoScope = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const OWNERS = ["io", "moglie", "tutti"];

  function walletKey(row) {
    return (row.owner || "") + "::" + (row.label || "Portafoglio");
  }

  function applyOwner(state, owner) {
    return { owner: OWNERS.indexOf(owner) >= 0 ? owner : "tutti", wallet: "" };
  }

  function selectWallet(state, key) {
    return { owner: state.owner || "tutti", wallet: key || "" };
  }

  function clearWallet(state) {
    return { owner: state.owner || "tutti", wallet: "" };
  }

  function inScope(state, row) {
    if (state.wallet) return walletKey(row) === state.wallet;
    if (state.owner && state.owner !== "tutti" && row.owner !== state.owner) return false;
    return true;
  }

  function persistQuery(state) {
    const params = new URLSearchParams();
    if (state.owner && state.owner !== "tutti") params.set("vista", state.owner);
    if (state.wallet) params.set("portafoglio", state.wallet);
    return params.toString();
  }

  function readScope(search, stored) {
    const params = new URLSearchParams(search || "");
    let owner = params.get("vista") || "";
    let wallet = params.get("portafoglio") || "";
    const fromUrl = params.has("vista") || params.has("portafoglio");
    if (!fromUrl && stored) {
      try {
        const parsed = JSON.parse(stored);
        owner = parsed.owner || "";
        wallet = parsed.wallet || "";
      } catch (err) {
        owner = "";
        wallet = "";
      }
    }
    if (OWNERS.indexOf(owner) < 0) owner = "tutti";
    return { owner: owner, wallet: wallet || "" };
  }

  return { walletKey, applyOwner, selectWallet, clearWallet, inScope, persistQuery, readScope };
});
