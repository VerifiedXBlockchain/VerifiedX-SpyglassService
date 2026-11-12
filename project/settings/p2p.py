from project.settings.environment import ENV

# P2P shops (p2p app, /api/p2p/). While P2P_DRY_RUN is on, the dry-run chain
# endpoints (/api/p2p/dry-run/...) take the unsigned description of a shop
# transaction and apply what the indexer would apply when the real one lands;
# with it off they answer 404. Spyglass never broadcasts a shop transaction
# either way.
P2P_DRY_RUN = ENV.bool("P2P_DRY_RUN", default=True)
