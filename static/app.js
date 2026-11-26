const icons = {
  Vendedor: '<path d="M4 7h16l-1 14H5zM8 8V6a4 4 0 0 1 8 0v2"/>',
  Gerente: '<path d="M12 2l8 3v6c0 5-4 8-8 11-4-3-8-6-8-11V5zM8 12l3 3 5-6"/>',
  Almoxarifado: '<path d="M2 10l10-7 10 7v11H2zM7 21v-9h10v9M7 16h10"/>',
  Transportadora:
    '<path d="M2 5h12v12H2zM14 9h4l4 4v4h-8"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="18" r="2"/>',
  users:
    '<circle cx="9" cy="7" r="3"/><path d="M2 21v-3a7 7 0 0 1 14 0v3M16 4a3 3 0 0 1 0 6M18 13q4 1 4 7"/>',
  exit: '<path d="M11 3H4v18h7M9 12h13m-5-5 5 5-5 5"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  shop: '<path d="M3 10l2-6h14l2 6M3 10q2 5 5 0 4 5 8 0 3 5 5 0M5 13v8h14v-8M9 21v-6h6v6"/>',
  box: '<path d="M3 7l9-5 9 5v10l-9 5-9-5zM3 7l9 5 9-5M12 12v10M7 5l10 5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  search: '<circle cx="10" cy="10" r="7"/><path d="M16 16l5 5"/>',
  pin: '<path d="M19 9c0 5-7 12-7 12S5 14 5 9a7 7 0 1 1 14 0Z"/><circle cx="12" cy="9" r="2"/>',
};
for (const icon of document.querySelectorAll("[data-icon]")) {
  icon.innerHTML =
    '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
    icons[icon.dataset.icon] +
    "</svg>";
}
for (const button of document.querySelectorAll("[data-open]")) {
  button.addEventListener("click", () =>
    document.getElementById(button.dataset.open).showModal(),
  );
}
for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => button.closest("dialog").close());
}
for (const select of document.querySelectorAll('select[name="stock"]')) {
  select.addEventListener("change", () => {
    select.form.elements.quantity.max = select.selectedOptions[0].dataset.max;
  });
}
