# Scratch model: can a deployment splice a component into a connection an imported subtopology already makes?
# Mirrors ComCcsds.TmTcFraming.Uplink (frameAccumulator.dataOut -> tcDeframer.dataIn) in miniature.
port Data(x: U32)

passive component Acc { output port dataOut: Data }
passive component Def { sync input port dataIn: Data }
passive component Farm { sync input port dataIn: Data
                         output port dataOut: Data }

module Lib {
  instance acc: Acc base id 0x100
  instance deframer: Def base id 0x200
  topology Framing {
    instance acc
    instance deframer
    connections Uplink { acc.dataOut -> deframer.dataIn }
  }
}

instance farm: Farm base id 0x300

topology Deploy {
  import Lib.Framing
  instance farm
  connections Splice {
    Lib.acc.dataOut -> farm.dataIn
    farm.dataOut -> Lib.deframer.dataIn
  }
}
