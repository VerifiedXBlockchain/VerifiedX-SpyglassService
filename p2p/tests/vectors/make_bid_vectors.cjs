// Writes bid_signatures.json: bid signatures made by vfx-web-sdk for keys
// that exist only for these tests, so the Python verifier is checked against
// the SDK's own signing code. No network: the client's base URL is a closed
// local port and signMessage makes no request.
//
//   node p2p/tests/vectors/make_bid_vectors.cjs <path to an installed vfx-web-sdk> > p2p/tests/vectors/bid_signatures.json
//
// The keys are SHA-256 of public labels. Never send funds to their addresses.

const crypto = require('crypto');
const path = require('path');

const sdkPath = process.argv[2];
if (!sdkPath) {
  process.stderr.write('usage: node make_bid_vectors.cjs <vfx-web-sdk directory>\n');
  process.exit(2);
}
const sdk = require(path.resolve(sdkPath, 'lib/cjs/index.js'));
const sdkVersion = require(path.resolve(sdkPath, 'package.json')).version;

function testKey(label) {
  return crypto.createHash('sha256').update(label, 'utf8').digest('hex');
}

// Core's bid message: {PurchaseKey}_{amount in 1e-8 units}_{BidAddress}
// (Models/DST/Bid.cs VerifyBidSignature). Amounts are decimal strings with
// up to 8 places, converted without floating point.
function units(amount) {
  const [whole, fraction = ''] = amount.split('.');
  return (BigInt(whole) * 100000000n + BigInt((fraction + '00000000').slice(0, 8))).toString();
}

async function main() {
  const cases = [
    { key: 'spyglass-p2p-test-key-1', network: 'testnet', purchaseKey: 'QwErTyUiOp1762900000', amount: '12.5' },
    { key: 'spyglass-p2p-test-key-1', network: 'testnet', purchaseKey: 'QwErTyUiOp1762900000', amount: '1' },
    { key: 'spyglass-p2p-test-key-2', network: 'testnet', purchaseKey: 'AsDfGhJkLz1762900123', amount: '0.00000001' },
    { key: 'spyglass-p2p-test-key-2', network: 'testnet', purchaseKey: 'AsDfGhJkLz1762900123', amount: '123456.78901234' },
    { key: 'spyglass-p2p-test-key-3', network: 'testnet', purchaseKey: 'ZxCvBnMqWe1762901000', amount: '250' },
    { key: 'spyglass-p2p-test-key-4', network: 'mainnet', purchaseKey: 'PoIuYtReWq1762902000', amount: '38.12345678' },
  ];

  const vectors = [];
  for (const c of cases) {
    const client = new sdk.VfxClient(c.network, { baseUrl: 'http://127.0.0.1:9' });
    const privateKey = testKey(c.key);
    const address = client.addressFromPrivate(privateKey);
    const message = `${c.purchaseKey}_${units(c.amount)}_${address}`;
    const viaSignMessage = await client.signMessage({ privateKey }, message);
    const viaGetSignature = client.getSignature(message, privateKey);
    if (viaSignMessage !== viaGetSignature) {
      throw new Error(`signMessage and getSignature disagree for ${c.key}`);
    }
    if (!sdk.verifySignature(message, viaSignMessage, address)) {
      throw new Error(`the SDK does not verify its own signature for ${c.key}`);
    }
    vectors.push({
      key_label: c.key,
      private_key: privateKey,
      network: c.network,
      address,
      purchase_key: c.purchaseKey,
      amount: c.amount,
      message,
      signature: viaSignMessage,
    });
  }

  process.stdout.write(
    JSON.stringify({ generated_by: `vfx-web-sdk ${sdkVersion} signMessage`, vectors }, null, 2) + '\n',
  );
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exit(1);
});
