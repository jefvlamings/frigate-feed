// Placeholder: replaced by the port of reolink-feed-card.js.
class FrigateFeedCard extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this.innerHTML = "<ha-card header=\"Frigate Feed\"><div class=\"card-content\">Card not built yet.</div></ha-card>";
  }

  set hass(_hass) {}

  getCardSize() {
    return 1;
  }
}

if (!customElements.get("frigate-feed-card")) {
  customElements.define("frigate-feed-card", FrigateFeedCard);
}
