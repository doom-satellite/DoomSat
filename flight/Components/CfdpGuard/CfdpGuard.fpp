module DoomMission {

    @ The CFDP uplink's gatekeeper, on both of cfdpManager's paths.
    @ Up (router -> cfdpManager): a Metadata PDU whose destination is not an uplinked WAD in the uplink directory
    @ (NAME.wad[.<nonce>].part, see Doom/WadPath.hpp) never reaches cfdpManager, so it opens no destination anywhere
    @ else. Class 2 file data that comes without an admitted Metadata is still staged in cfdpManager's tmp_dir as
    @ <eid>:<seq>.tmp, which the repo's parameters put in the uplink directory (.cfdp-tmp); class 1 file data with
    @ no Metadata is dropped unwritten.
    @ Down (cfdpManager -> com queue): when cfdpManager's own FIN for an upload the guard let through says the file
    @ arrived whole (no error, retained), the guard announces the file to the Doom component, which renames it to
    @ NAME.wad. The commit happens on board, with no ground command.
    @ Every PDU is read with F´'s own CFDP classes, the ones cfdpManager reads it with, so the two cannot disagree.
    passive component CfdpGuard {

        # ----------------------------------------------------------------------
        # Ports
        # ----------------------------------------------------------------------

        @ PDUs from ComCcsds.fprimeRouter.fileOut: the FW_PACKET_FILE descriptor, then the PDU
        sync input port uplinkIn: Fw.BufferSend

        @ Every PDU on to cfdpManager.dataIn[0]. A refused Metadata goes too, with its descriptor changed so that
        @ cfdpManager hands the buffer back unread: handing it back to the router from here would deadlock, because
        @ the router is still inside its guarded dataIn.
        output port uplinkOut: Fw.BufferSend

        @ PDUs from cfdpManager.dataOut, one port per channel
        sync input port downlinkIn: [Svc.Ccsds.Cfdp.NumChannels] Fw.BufferSend

        @ The same PDUs, unchanged, on to ComCcsds.comQueue.bufferQueueIn[FILE]
        output port downlinkOut: [Svc.Ccsds.Cfdp.NumChannels] Fw.BufferSend

        @ An upload that arrived whole (to doom.fileAnnounce)
        output port fileAnnounceOut: Svc.FileAnnounce

        # ----------------------------------------------------------------------
        # Events and telemetry
        # ----------------------------------------------------------------------

        event UploadRefused(dest: string size 120, srcEid: U32, seq: U32) \
            severity warning high \
            id 0 \
            format "CFDP upload to {} refused (transaction {}:{}): only NAME.wad[.<nonce>].part in the uplink directory"

        event MetadataUnreadable(status: I32) \
            severity warning low \
            id 1 \
            format "CFDP Metadata that cfdpManager cannot read either (status {}): refused" \
            throttle 10

        event UploadCommitted(dest: string size 120, srcEid: U32, seq: U32) \
            severity activity high \
            id 2 \
            format "CFDP upload {} arrived whole (transaction {}:{}): putting it in place"

        event UploadNotCommitted(dest: string size 120, srcEid: U32, seq: U32, conditionCode: U8, fileStatus: U8) \
            severity warning low \
            id 3 \
            format "CFDP upload {} (transaction {}:{}) ended with condition code {}, file status {}: left as it is"

        event UploadForgotten(dest: string size 120, srcEid: U32, seq: U32) \
            severity warning low \
            id 4 \
            format "CFDP upload table full: {} (transaction {}:{}) is no longer followed, so its FIN will not put it in place; COMMIT_WAD can"

        @ Uploads let through to the uplink directory (class 1 included; a resent Metadata is not counted again)
        telemetry UPLOADS_ACCEPTED: U32 id 0

        @ Uploads refused for their destination, or for a Metadata nobody can read
        telemetry UPLOADS_REFUSED: U32 id 1

        @ Uploads announced to the Doom component on the receiver's FIN; its WadUplinked or WadUplinkFailed says how
        @ the rename went
        telemetry UPLOADS_COMMITTED: U32 id 2

        # ----------------------------------------------------------------------
        # Standard ports
        # ----------------------------------------------------------------------

        time get port timeCaller
        event port eventOut
        text event port textEventOut
        telemetry port tlmOut
    }
}
