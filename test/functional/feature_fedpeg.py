#!/usr/bin/env python3

import time

from test_framework.authproxy import JSONRPCException
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    connect_nodes_bi,
    get_auth_cookie,
    get_datadir_path,
    rpc_port,
    p2p_port,
    assert_raises_rpc_error,
    assert_equal,
    bytes_to_hex_str,
)
from test_framework import util
from test_framework.messages import (
    CBlock,
    CTransaction,
    CTxInWitness,
    FromHex,
)
from test_framework.blocktools import (
    add_witness_commitment,
)
from decimal import Decimal

def get_new_unconfidential_address(node, addr_type="p2sh-segwit"):
    addr = node.getnewaddress("", addr_type)
    val_addr = node.getaddressinfo(addr)
    if 'unconfidential' in val_addr:
        return val_addr['unconfidential']
    return val_addr['address']

class FedPegTest(BitcoinTestFramework):

    def set_test_params(self):
        self.setup_clean_chain = True
        self.num_nodes = 4

    def add_options(self, parser):
        parser.add_argument("--parent_binpath", dest="parent_binpath", default="",
                            help="Use a different binary for launching nodes")
        parser.add_argument("--parent_bitcoin", dest="parent_bitcoin", default=False, action="store_true",
                            help="Parent nodes are Bitcoin")

    def skip_test_if_missing_module(self):
        self.skip_if_no_wallet()

    def setup_network(self, split=False):
        if self.options.parent_bitcoin and self.options.parent_binpath == "":
            raise Exception("Can't run with --parent_bitcoin without specifying --parent_binpath")

        self.nodes = []
        # Setup parent nodes
        parent_chain = "parent" if not self.options.parent_bitcoin else "regtest"
        parent_binary = [self.options.parent_binpath] if self.options.parent_binpath != "" else None
        for n in range(2):
            extra_args = [
                "-port="+str(p2p_port(n)),
                "-rpcport="+str(rpc_port(n))
            ]
            if self.options.parent_bitcoin:
                # bitcoind can't read elements.conf config files
                extra_args.extend([
                    "-regtest=1",
                    "-printtoconsole=0",
                    "-server=1",
                    "-discover=0",
                    "-keypool=1",
                    "-listenonion=0",
                    "-addresstype=legacy", # To make sure bitcoind gives back p2pkh no matter version
                ])
            else:
                extra_args.extend([
                    "-validatepegin=0",
                    "-initialfreecoins=0",
                    "-anyonecanspendaremine",
                    "-signblockscript=51", # OP_TRUE
                    '-con_blocksubsidy=5000000000',
                    "-pubkeyprefix=111",
                    "-scriptprefix=196",
                ])

            self.add_nodes(1, [extra_args], chain=[parent_chain], binary=parent_binary, chain_in_args=[not self.options.parent_bitcoin])
            self.start_node(n)
            print("Node {} started".format(n))

        connect_nodes_bi(self.nodes, 0, 1)
        self.parentgenesisblockhash = self.nodes[0].getblockhash(0)
        if not self.options.parent_bitcoin:
            parent_pegged_asset = self.nodes[0].getsidechaininfo()['pegged_asset']

        # Setup sidechain nodes
        self.fedpeg_script = "512103dff4923d778550cc13ce0d887d737553b4b58f4e8e886507fc39f5e447b2186451ae"
        for n in range(2):
            extra_args = [
                "-printtoconsole=0",
                "-port="+str(p2p_port(2+n)),
                "-rpcport="+str(rpc_port(2+n)),
                '-parentgenesisblockhash=%s' % self.parentgenesisblockhash,
                '-validatepegin=1',
                '-fedpegscript=%s' % self.fedpeg_script,
                '-anyonecanspendaremine=0',
                '-minrelaytxfee=0',
                '-blockmintxfee=0',
                '-initialfreecoins=0',
                '-peginconfirmationdepth=10',
                '-mainchainrpchost=127.0.0.1',
                '-mainchainrpcport=%s' % rpc_port(n),
                '-recheckpeginblockinterval=15', # Long enough to allow failure and repair before timeout
                '-parentpubkeyprefix=111',
                '-parentscriptprefix=196',
                # Turn of consistency checks that can cause assert when parent node stops
                # and a peg-in transaction fails this belt-and-suspenders check.
                '-checkmempool=0',
            ]
            if not self.options.parent_bitcoin:
                extra_args.extend([
                    '-parentpubkeyprefix=111',
                    '-parentscriptprefix=196',
                    "-parent_bech32_hrp=ert",
                    '-con_parent_chain_signblockscript=51',
                    '-con_parent_pegged_asset=%s' % parent_pegged_asset,
                ])

            # Use rpcuser auth only for first parent.
            if n==0:
                # Extract username and password from cookie file and use directly.
                datadir = get_datadir_path(self.options.tmpdir, n)
                rpc_u, rpc_p = get_auth_cookie(datadir, parent_chain)
                extra_args.extend([
                    '-mainchainrpcuser=%s' % rpc_u,
                    '-mainchainrpcpassword=%s' % rpc_p,
                ])
            else:
                # Need to specify where to find parent cookie file
                datadir = get_datadir_path(self.options.tmpdir, n)
                extra_args.append('-mainchainrpccookiefile='+datadir+"/" + parent_chain + "/.cookie")

            self.add_nodes(1, [extra_args], chain=["sidechain"])
            self.start_node(2+n)
            print("Node {} started".format(2+n))

        # We only connect the same-chain nodes, so sync_all works correctly
        connect_nodes_bi(self.nodes, 2, 3)
        self.node_groups = [[self.nodes[0], self.nodes[1]], [self.nodes[2], self.nodes[3]]]
        self.sync_all(self.node_groups)
        print("Setting up network done")

    def test_pegout(self, parent_chain_addr, sidechain):
        pegout_txid = sidechain.sendtomainchain(parent_chain_addr, 1)
        raw_pegout = sidechain.getrawtransaction(pegout_txid, True)
        assert 'vout' in raw_pegout and len(raw_pegout['vout']) > 0
        pegout_tested = False
        for output in raw_pegout['vout']:
            scriptPubKey = output['scriptPubKey']
            if 'type' in scriptPubKey and scriptPubKey['type'] == 'nulldata':
                assert ('pegout_hex' in scriptPubKey and 'pegout_asm' in scriptPubKey and 'pegout_type' in scriptPubKey)
                assert ('pegout_chain' in scriptPubKey and 'pegout_reqSigs' in scriptPubKey and 'pegout_addresses' in scriptPubKey)
                assert scriptPubKey['pegout_chain'] == self.parentgenesisblockhash
                assert scriptPubKey['pegout_reqSigs'] == 1
                assert parent_chain_addr in scriptPubKey['pegout_addresses']
                pegout_tested = True
                break
        assert pegout_tested
        sidechain.generatetoaddress(1, sidechain.getnewaddress())
        assert_equal(sidechain.gettransaction(pegout_txid)["confirmations"], 1)

    def run_test(self):
        parent = self.nodes[0]
        #parent2 = self.nodes[1]
        sidechain = self.nodes[2]
        sidechain2 = self.nodes[3]
        util.node_fastmerkle = sidechain

        parent.generate(101)
        sidechain.generate(101)

        addrs = sidechain.getpeginaddress()
        addr = addrs["mainchain_address"]
        assert_equal(sidechain.decodescript(addrs["claim_script"])["type"], "witness_v0_keyhash")
        txid1 = parent.sendtoaddress(addr, 24)
        # 10+2 confirms required to get into mempool and confirm
        parent.generate(1)
        time.sleep(2)
        proof = parent.gettxoutproof([txid1])

        raw = parent.getrawtransaction(txid1)

        print("Attempting peg-ins")
        # First attempt fails the consensus check but gives useful result
        try:
            pegtxid = sidechain.claimpegin(raw, proof)
            raise Exception("Peg-in should not be mature enough yet, need another block.")
        except JSONRPCException as e:
            assert("Peg-in Bitcoin transaction needs more confirmations to be sent." in e.error["message"])

        # Second attempt simply doesn't hit mempool bar
        parent.generate(10)
        try:
            pegtxid = sidechain.claimpegin(raw, proof)
            raise Exception("Peg-in should not be mature enough yet, need another block.")
        except JSONRPCException as e:
            assert("Peg-in Bitcoin transaction needs more confirmations to be sent." in e.error["message"])

        try:
            pegtxid = sidechain.createrawpegin(raw, proof, 'AEIOU')
            raise Exception("Peg-in with non-hex claim_script should fail.")
        except JSONRPCException as e:
            assert("Given claim_script is not hex." in e.error["message"])

        # Should fail due to non-matching wallet address
        try:
            scriptpubkey = sidechain.getaddressinfo(get_new_unconfidential_address(sidechain))["scriptPubKey"]
            pegtxid = sidechain.claimpegin(raw, proof, scriptpubkey)
            raise Exception("Peg-in with non-matching claim_script should fail.")
        except JSONRPCException as e:
            assert("Given claim_script does not match the given Bitcoin transaction." in e.error["message"])

        # 12 confirms allows in mempool
        parent.generate(1)

        # Make sure that a tx with a duplicate pegin claim input gets rejected.
        raw_pegin = sidechain.createrawpegin(raw, proof)["hex"]
        raw_pegin = FromHex(CTransaction(), raw_pegin)
        raw_pegin.vin.append(raw_pegin.vin[0]) # duplicate the pegin input
        raw_pegin = sidechain.signrawtransactionwithwallet(raw_pegin.serialize().hex())["hex"]
        assert_raises_rpc_error(-26, "bad-txns-inputs-duplicate", sidechain.sendrawtransaction, raw_pegin)
        # Also try including this tx in a block manually and submitting it.
        doublespendblock = FromHex(CBlock(), sidechain.getnewblockhex())
        doublespendblock.vtx.append(FromHex(CTransaction(), raw_pegin))
        doublespendblock.hashMerkleRoot = doublespendblock.calc_merkle_root()
        add_witness_commitment(doublespendblock)
        doublespendblock.solve()
        block_hex = bytes_to_hex_str(doublespendblock.serialize(True))
        assert_raises_rpc_error(-25, "bad-txns-inputs-duplicate", sidechain.testproposedblock, block_hex, True)

        # Should succeed via wallet lookup for address match, and when given
        raw_pegin = sidechain.createrawpegin(raw, proof)['hex']
        signed_pegin = sidechain.signrawtransactionwithwallet(raw_pegin)

        sample_pegin_struct = FromHex(CTransaction(), signed_pegin["hex"])
        # Round-trip peg-in transaction using python serialization
        assert_equal(signed_pegin["hex"], sample_pegin_struct.serialize().hex())
        # Store this for later (evil laugh)
        sample_pegin_witness = sample_pegin_struct.wit.vtxinwit[0].peginWitness

        pegtxid1 = sidechain.claimpegin(raw, proof)
        # Make sure a second pegin claim does not get accepted in the mempool when
        # another mempool tx already claims that pegin.
        assert_raises_rpc_error(-4, "txn-mempool-conflict", sidechain.claimpegin, raw, proof)

        # Will invalidate the block that confirms this transaction later
        self.sync_all(self.node_groups)
        blockhash = sidechain2.generate(1)
        self.sync_all(self.node_groups)
        sidechain.generate(5)

        tx1 = sidechain.gettransaction(pegtxid1)

        if "confirmations" in tx1 and tx1["confirmations"] == 6:
            print("Peg-in is confirmed: Success!")
        else:
            raise Exception("Peg-in confirmation has failed.")

        # Look at pegin fields
        decoded = sidechain.decoderawtransaction(tx1["hex"])
        assert decoded["vin"][0]["is_pegin"] == True
        assert len(decoded["vin"][0]["pegin_witness"]) > 0
        # Check that there's sufficient fee for the peg-in
        vsize = decoded["vsize"]
        fee_output = decoded["vout"][1]
        fallbackfee_pervbyte = Decimal("0.00001")/Decimal("1000")
        assert fee_output["scriptPubKey"]["type"] == "fee"
        assert fee_output["value"] >= fallbackfee_pervbyte*vsize

        # Quick reorg checks of pegs
        sidechain.invalidateblock(blockhash[0])
        if sidechain.gettransaction(pegtxid1)["confirmations"] != 0:
            raise Exception("Peg-in didn't unconfirm after invalidateblock call.")

        # Create duplicate claim, put it in block along with current one in mempool
        # to test duplicate-in-block claims between two txs that are in the same block.
        raw_pegin = sidechain.createrawpegin(raw, proof)["hex"]
        raw_pegin = sidechain.signrawtransactionwithwallet(raw_pegin)["hex"]
        raw_pegin = FromHex(CTransaction(), raw_pegin)
        doublespendblock = FromHex(CBlock(), sidechain.getnewblockhex())
        assert(len(doublespendblock.vtx) == 2) # coinbase and pegin
        doublespendblock.vtx.append(raw_pegin)
        doublespendblock.hashMerkleRoot = doublespendblock.calc_merkle_root()
        add_witness_commitment(doublespendblock)
        doublespendblock.solve()
        block_hex = bytes_to_hex_str(doublespendblock.serialize(True))
        assert_raises_rpc_error(-25, "bad-txns-double-pegin", sidechain.testproposedblock, block_hex, True)

        # Re-enters block
        sidechain.generate(1)
        if sidechain.gettransaction(pegtxid1)["confirmations"] != 1:
            raise Exception("Peg-in should have one confirm on side block.")
        sidechain.reconsiderblock(blockhash[0])
        if sidechain.gettransaction(pegtxid1)["confirmations"] != 6:
            raise Exception("Peg-in should be back to 6 confirms.")

        # Now the pegin is already claimed in a confirmed tx.
        # In that case, a duplicate claim should (1) not be accepted in the mempool
        # and (2) not be accepted in a block.
        assert_raises_rpc_error(-4, "pegin-already-claimed", sidechain.claimpegin, raw, proof)
        # For case (2), manually craft a block and include the tx.
        doublespendblock = FromHex(CBlock(), sidechain.getnewblockhex())
        doublespendblock.vtx.append(raw_pegin)
        doublespendblock.hashMerkleRoot = doublespendblock.calc_merkle_root()
        add_witness_commitment(doublespendblock)
        doublespendblock.solve()
        block_hex = bytes_to_hex_str(doublespendblock.serialize(True))
        assert_raises_rpc_error(-25, "bad-txns-double-pegin", sidechain.testproposedblock, block_hex, True)

        # Do multiple claims in mempool
        n_claims = 6

        print("Flooding mempool with a few claims")
        pegtxs = []
        sidechain.generate(101)

        # Do mixture of raw peg-in and automatic peg-in tx construction
        # where raw creation is done on another node
        for i in range(n_claims):
            addrs = sidechain.getpeginaddress()
            txid = parent.sendtoaddress(addrs["mainchain_address"], 1)
            parent.generate(1)
            proof = parent.gettxoutproof([txid])
            raw = parent.getrawtransaction(txid)
            if i % 2 == 0:
                parent.generate(11)
                pegtxs += [sidechain.claimpegin(raw, proof)]
            else:
                # The raw API doesn't check for the additional 2 confirmation buffer
                # So we only get 10 confirms then send off. Miners will add to block anyways.

                # Don't mature whole way yet to test signing immature peg-in input
                parent.generate(8)
                # Wallet in sidechain2 gets funds instead of sidechain
                raw_pegin = sidechain2.createrawpegin(raw, proof, addrs["claim_script"])["hex"]
                # First node should also be able to make a valid transaction with or without 3rd arg
                # since this wallet originated the claim_script itself
                sidechain.createrawpegin(raw, proof, addrs["claim_script"])
                sidechain.createrawpegin(raw, proof)
                signed_pegin = sidechain.signrawtransactionwithwallet(raw_pegin)
                assert(signed_pegin["complete"])
                assert("warning" in signed_pegin) # warning for immature peg-in
                # fully mature them now
                parent.generate(1)
                pegtxs += [sidechain.sendrawtransaction(signed_pegin["hex"])]

        self.sync_all(self.node_groups)
        sidechain2.generate(1)
        for i, pegtxid in enumerate(pegtxs):
            if i % 2 == 0:
                tx = sidechain.gettransaction(pegtxid)
            else:
                tx = sidechain2.gettransaction(pegtxid)
            if "confirmations" not in tx or tx["confirmations"] == 0:
                raise Exception("Peg-in confirmation has failed.")

        print("Test pegouts")
        self.test_pegout(get_new_unconfidential_address(parent, "legacy"), sidechain)
        self.test_pegout(get_new_unconfidential_address(parent, "p2sh-segwit"), sidechain)
        self.test_pegout(get_new_unconfidential_address(parent, "bech32"), sidechain)

        print("Test pegout P2SH")
        parent_chain_addr = get_new_unconfidential_address(parent)
        parent_pubkey = parent.getaddressinfo(parent_chain_addr)["pubkey"]
        parent_chain_p2sh_addr = parent.createmultisig(1, [parent_pubkey])["address"]
        self.test_pegout(parent_chain_p2sh_addr, sidechain)

        print("Test pegout Garbage")
        parent_chain_addr = "garbage"
        try:
            self.test_pegout(parent_chain_addr, sidechain)
            raise Exception("A garbage address should fail.")
        except JSONRPCException as e:
            assert("Invalid Bitcoin address" in e.error["message"])

        print("Test pegout Garbage valid")
        prev_txid = sidechain.sendtoaddress(sidechain.getnewaddress(), 1)
        sidechain.generate(1)
        pegout_chain = 'a' * 64
        pegout_hex = 'b' * 500
        inputs = [{"txid": prev_txid, "vout": 0}]
        outputs = {"vdata": [pegout_chain, pegout_hex]}
        rawtx = sidechain.createrawtransaction(inputs, outputs)
        raw_pegout = sidechain.decoderawtransaction(rawtx)

        assert 'vout' in raw_pegout and len(raw_pegout['vout']) > 0
        pegout_tested = False
        for output in raw_pegout['vout']:
            scriptPubKey = output['scriptPubKey']
            if 'type' in scriptPubKey and scriptPubKey['type'] == 'nulldata':
                assert ('pegout_hex' in scriptPubKey and 'pegout_asm' in scriptPubKey and 'pegout_type' in scriptPubKey)
                assert ('pegout_chain' in scriptPubKey and 'pegout_reqSigs' not in scriptPubKey and 'pegout_addresses' not in scriptPubKey)
                assert scriptPubKey['pegout_type'] == 'nonstandard'
                assert scriptPubKey['pegout_chain'] == pegout_chain
                assert scriptPubKey['pegout_hex'] == pegout_hex
                pegout_tested = True
                break
        assert pegout_tested

        print("Now test failure to validate peg-ins based on intermittent bitcoind rpc failure")
        self.stop_node(1)
        txid = parent.sendtoaddress(addr, 1)
        parent.generate(12)
        proof = parent.gettxoutproof([txid])
        raw = parent.getrawtransaction(txid)
        sidechain.claimpegin(raw, proof) # stuck peg
        sidechain.generate(1)
        print("Waiting to ensure block is being rejected by sidechain2")
        time.sleep(5)

        assert(sidechain.getblockcount() != sidechain2.getblockcount())

        print("Restarting parent2")
        self.start_node(1)
        connect_nodes_bi(self.nodes, 0, 1)

        # Don't make a block, race condition when pegin-invalid block
        # is awaiting further validation, nodes reject subsequent blocks
        # even ones they create
        print("Now waiting for node to re-evaluate peg-in witness failed block... should take a few seconds")
        self.sync_all(self.node_groups)
        print("Completed!\n")
        print("Now send funds out in two stages, partial, and full")
        some_btc_addr = get_new_unconfidential_address(parent)
        bal_1 = sidechain.getwalletinfo()["balance"]['bitcoin']
        try:
            sidechain.sendtomainchain(some_btc_addr, bal_1 + 1)
            raise Exception("Sending out too much; should have failed")
        except JSONRPCException as e:
            assert("Insufficient funds" in e.error["message"])

        assert(sidechain.getwalletinfo()["balance"]["bitcoin"] == bal_1)
        try:
            sidechain.sendtomainchain(some_btc_addr+"b", bal_1 - 1)
            raise Exception("Sending to invalid address; should have failed")
        except JSONRPCException as e:
            assert("Invalid Bitcoin address" in e.error["message"])

        assert(sidechain.getwalletinfo()["balance"]["bitcoin"] == bal_1)
        try:
            sidechain.sendtomainchain("1Nro9WkpaKm9axmcfPVp79dAJU1Gx7VmMZ", bal_1 - 1)
            raise Exception("Sending to mainchain address when should have been testnet; should have failed")
        except JSONRPCException as e:
            assert("Invalid Bitcoin address" in e.error["message"])

        assert(sidechain.getwalletinfo()["balance"]["bitcoin"] == bal_1)

        # Test superfluous peg-in witness data on regular spend before we have no funds
        raw_spend = sidechain.createrawtransaction([], {sidechain.getnewaddress():1})
        fund_spend = sidechain.fundrawtransaction(raw_spend)
        sign_spend = sidechain.signrawtransactionwithwallet(fund_spend["hex"])
        signed_struct = FromHex(CTransaction(), sign_spend["hex"])
        # Non-witness tx has no witness serialized yet
        if len(signed_struct.wit.vtxinwit) == 0:
            signed_struct.wit.vtxinwit = [CTxInWitness()]
        signed_struct.wit.vtxinwit[0].peginWitness.stack = sample_pegin_witness.stack
        assert_equal(sidechain.testmempoolaccept([signed_struct.serialize().hex()])[0]["allowed"], False)
        assert_equal(sidechain.testmempoolaccept([signed_struct.serialize().hex()])[0]["reject-reason"], "68: extra-pegin-witness")
        signed_struct.wit.vtxinwit[0].peginWitness.stack = [b'\x00'*100000] # lol
        assert_equal(sidechain.testmempoolaccept([signed_struct.serialize().hex()])[0]["allowed"], False)
        assert_equal(sidechain.testmempoolaccept([signed_struct.serialize().hex()])[0]["reject-reason"], "68: extra-pegin-witness")

        peg_out_txid = sidechain.sendtomainchain(some_btc_addr, 1)

        peg_out_details = sidechain.decoderawtransaction(sidechain.getrawtransaction(peg_out_txid))
        # peg-out, change, fee
        assert(len(peg_out_details["vout"]) == 3)
        found_pegout_value = False
        for output in peg_out_details["vout"]:
            if "value" in output and output["value"] == 1:
                found_pegout_value = True
        assert(found_pegout_value)

        bal_2 = sidechain.getwalletinfo()["balance"]["bitcoin"]
        # Make sure balance went down
        assert(bal_2 + 1 < bal_1)

        # Send rest of coins using subtractfee from output arg
        sidechain.sendtomainchain(some_btc_addr, bal_2, True)

        assert(sidechain.getwalletinfo()["balance"]['bitcoin'] == 0)

        print('Test coinbase peg-in maturity rules')

        # Have bitcoin output go directly into a claim output
        pegin_info = sidechain.getpeginaddress()
        mainchain_addr = pegin_info["mainchain_address"]
        # Watch the address so we can get tx without txindex
        parent.importaddress(mainchain_addr)
        claim_block = parent.generatetoaddress(50, mainchain_addr)[0]
        block_coinbase = parent.getblock(claim_block, 2)["tx"][0]
        claim_txid = block_coinbase["txid"]
        claim_tx = block_coinbase["hex"]
        claim_proof = parent.gettxoutproof([claim_txid], claim_block)

        # Can't claim something even though it has 50 confirms since it's coinbase
        assert_raises_rpc_error(-8, "Peg-in Bitcoin transaction needs more confirmations to be sent.", sidechain.claimpegin, claim_tx, claim_proof)
        # If done via raw API, still doesn't work
        coinbase_pegin = sidechain.createrawpegin(claim_tx, claim_proof)
        assert_equal(coinbase_pegin["mature"], False)
        signed_pegin = sidechain.signrawtransactionwithwallet(coinbase_pegin["hex"])["hex"]
        assert_raises_rpc_error(-26, "bad-pegin-witness, Needs more confirmations.", sidechain.sendrawtransaction, signed_pegin)

        # 50 more blocks to allow wallet to make it succeed by relay and consensus
        parent.generatetoaddress(50, parent.getnewaddress())
        # Wallet still doesn't want to for 2 more confirms
        assert_equal(sidechain.createrawpegin(claim_tx, claim_proof)["mature"], False)
        # But we can just shoot it off
        claim_txid = sidechain.sendrawtransaction(signed_pegin)
        sidechain.generatetoaddress(1, sidechain.getnewaddress())
        assert_equal(sidechain.gettransaction(claim_txid)["confirmations"], 1)

        print('Success!')

        # Manually stop sidechains first, then the parent chains.
        self.stop_node(2)
        self.stop_node(3)
        self.stop_node(0)
        self.stop_node(1)

if __name__ == '__main__':
    FedPegTest().main()
