-- DoomSat: wire Yamcs CfdpService to F Prime v4.3.0 Svc/Ccsds/CfdpManager over the existing CCSDS links.
-- F Prime frames every CFDP PDU as: space packet on APID 3 (FW_PACKET_FILE) | U16 descriptor 0x0003 | raw PDU.
--
-- Downlink: keep APID 3 packets whose descriptor is 0x0003 and whose first PDU byte carries CFDP version '001'
-- (Fw::FilePacket uses the same APID and descriptor but its first byte is a type 0..3). Strip 6 + 2 bytes.
create stream cfdp_in as select substring(packet, 8) as pdu from tm_realtime where (extract_ushort(packet, 0) & 2047) = 3 and extract_ushort(packet, 6) = 3 and (extract_ushort(packet, 8) >> 13) = 1

-- Uplink: CfdpService writes (gentime, entityId, seqNum, pdu) tuples here.
create stream cfdp_out (gentime TIMESTAMP, entityId long, seqNum int, pdu binary)

-- Wrap each PDU as a TC space packet on APID 3 (0x1003 = TC type bit + APID 3), sequence flags 11 (0xC000),
-- length 0 (FprimeCommandPostprocessor fills length and the per-APID count), then descriptor 0x0003.
-- cfdp_tc is a dedicated TC stream so a second virtual channel can carry it at lower priority than tc_realtime.
create stream cfdp_tc (gentime TIMESTAMP, origin string, seqNum int, cmdName string, binary binary)
insert into cfdp_tc select gentime, 'cfdp-service' as origin, seqNum, '/doomsat/cfdp/pdu' as cmdName, unhex('1003C00000000003') + pdu as binary from cfdp_out
