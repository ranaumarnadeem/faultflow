// Port-only stub of the memory macro the chip fixture instantiates: the macro
// autoMBIST's collar fixture (tests/fixtures/autombist/input_demo_8x16_scn4m)
// wraps, here with the extra pins a 1rw1r macro has -- a write mask on port 0
// and a read-only port 1. Read with `read_verilog -lib`.
(* blackbox *)
module input_demo_8x16_scn4m #(
    parameter integer ADDR_WIDTH = 4,
    parameter integer DATA_WIDTH = 8,
    parameter integer NUM_SPARE_ROWS = 0,
    parameter integer NUM_SPARE_COLS = 0
) (
    input  wire       clk0,
    input  wire       csb0,
    input  wire       web0,
    input  wire       wmask0,
    input  wire [3:0] addr0,
    input  wire [7:0] din0,
    output wire [7:0] dout0,
    input  wire       clk1,
    input  wire       csb1,
    input  wire [3:0] addr1,
    output wire [7:0] dout1
);
endmodule
