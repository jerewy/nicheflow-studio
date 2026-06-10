const niche = document.querySelector("#niche");
const status = document.querySelector("#status");

chrome.storage.sync.get({ niche: "history" }, (stored) => {
  niche.value = stored.niche;
});

document.querySelector("#save").addEventListener("click", () => {
  chrome.storage.sync.set({ niche: niche.value }, () => {
    status.textContent = `Saved. New captures will enter the ${niche.value} pool.`;
  });
});
