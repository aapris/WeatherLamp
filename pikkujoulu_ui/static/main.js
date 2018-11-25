function updateLastSeen() {
    var milliseconds = (new Date).getTime();
    $(".led").each(function (index) {
        var age = milliseconds - $(this).data('lastseen');
        var age_str = Number.parseFloat(age/1000).toFixed(1);
        $(this).find('.uptime').text('Seen: ' + age_str + ' sec ago');
        if (age > 10000) {$(this).addClass("outdated");}
        // console.log("AGE " + age);
    });
}
setInterval(updateLastSeen, 1000);

function highlight(element_id){
    $(element_id).addClass("highlight");
    setTimeout(function () {
          $(element_id).removeClass('highlight');
    }, 1000);  // Timeout should be the same which is in style.css' .highlight
}


var leds = {};
var socket = io.connect('http://' + document.domain + ':' + location.port);
socket.on('connect', function () {
    socket.emit('my event', {data: 'CONNECTAATIO ON TAPAHTUNUT'});
    $('#msg').append("CONNECTED!<br>");
});
socket.on('debug', function (data) {
    if (data === void (0)) {
        console.log("Got undefined data");
        return;
    }
    console.log("DEBUG " + data.type);
    console.log(data);
});

socket.on('ping', function (data) {
    if (data === void (0)) {
        console.log("Got undefined data");
        return;
    }
    console.log("PING " + data.type);
    console.log(data);
    var milliseconds = (new Date).getTime();
    var _id = data.dev;  // DIV's id value
    if (data.dev in leds) {
        console.log("Led device exists: " + data.dev);
    } else {
        console.log("New led device: " + data.dev);
        $('#msg').append("<div class='led' id='" + _id + "'>" + data.dev + "<br><span class='uptime'>Seen: 0.0 sec ago</span></div>");
        leds[data.dev] = 1;
    }
    console.log(leds);
    var leddiv = $('#' + _id);
    leddiv.data('lastseen', milliseconds);
    leddiv.removeClass("outdated");
    updateLastSeen();
    highlight('#' + _id);
});



$("button").on("click", function () {
    socket.emit('my event', {data: 'Button pressed!'});
});
